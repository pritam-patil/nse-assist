"""Headline retrieval for the sentiment layer — RSS only, no keys, no SLA.

Two kinds of source, unequal in what they can promise.

PER-SYMBOL: Google News RSS search. One request per symbol, scoped to India, which
is the only free way to ask "what was written about this company". It is a search
over an index nobody guarantees the composition of.

MARKET-WIDE: one or two Indian market feeds, fetched once per run and filtered by
symbol and company name. Cheaper than a search per symbol and catches items the
search misses, at the cost of only finding companies named in a headline.

NOTHING HERE IS RELIABLE AND NOTHING DEPENDS ON IT

Every function returns [] on failure rather than raising. That is not defensive
habit — it is the design. A brief without sentiment is a complete brief; a brief
that failed to send because a free RSS endpoint was slow is a broken one.

WHY THE HEADLINES ARE STORED AND NOT JUST THE SCORE

Google News does not serve history. A query run tomorrow for today's date returns
whatever the index holds tomorrow, which is not what it held today — items expire,
get reranked, and vanish. So the point-in-time record has to be built forward by
storing what was actually seen at fetch time. There is no way to reconstruct it
later, and that is the entire reason this stage runs every evening rather than
being backfilled once when someone wants to analyse it.
"""

import re
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from urllib.parse import quote_plus

import requests

from src import config

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
)

# Market-wide feeds, fetched once per run rather than per symbol. Both are public
# RSS with no key. If either dies the other still works, and if both die the stage
# no-ops.
MARKET_FEEDS = (
    ("economictimes", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("moneycontrol", "https://www.moneycontrol.com/rss/business.xml"),
)

# Enough to characterise a mood, few enough to keep the prompt small and the
# stage cheap. Ten headlines is already more signal than a daily bar carries.
MAX_HEADLINES_PER_SYMBOL = 10

# Anything older is describing a different situation. A week is generous for a
# swing horizon of ten sessions.
MAX_HEADLINE_AGE_DAYS = 7

FEED_TIMEOUT_SECONDS = 15

# A ticker alone is a bad search query when it is also an ordinary word or an
# abbreviation with a more famous owner. Only symbols where the ticker genuinely
# misleads are listed; everything else searches as "<SYMBOL> NSE share price",
# which is specific enough because the NSE qualifier does the disambiguating.
#
# EVERY KEY MUST BE A UNIVERSE SYMBOL — assert_consistent() checks it, because a
# typo'd key is silently inert: the symbol falls through to the generic query and
# nothing ever says the mapping was skipped.
#
# A wrong *name* is the worse failure and nothing can catch it automatically: it
# scores another company's news under this symbol, confidently. So this map covers
# the cases where the ticker demonstrably misfires — group tickers and ambiguous
# abbreviations — rather than aiming for all 100.
COMPANY_NAMES = {
    # Group tickers, where the short form names a family with several listed
    # entities. These are the ones that actually misfire: "RELIANCE NSE share
    # price" returns Reliance Infrastructure coverage, a different company.
    "RELIANCE": "Reliance Industries",
    "ADANIENT": "Adani Enterprises",
    "ADANIPORTS": "Adani Ports",
    "ADANIGREEN": "Adani Green Energy",
    "ADANIPOWER": "Adani Power",
    "ADANIENSOL": "Adani Energy Solutions",
    "BAJFINANCE": "Bajaj Finance",
    "BAJAJFINSV": "Bajaj Finserv",
    "BAJAJ-AUTO": "Bajaj Auto",
    "BAJAJHLDNG": "Bajaj Holdings",
    "TATASTEEL": "Tata Steel",
    "TATAPOWER": "Tata Power",
    "TATACONSUM": "Tata Consumer Products",
    "TMPV": "Tata Motors Passenger Vehicles",
    "TMCV": "Tata Motors commercial vehicles",
    "TCS": "Tata Consultancy Services",
    "TITAN": "Titan Company",
    "TRENT": "Trent Limited",
    "JSWSTEEL": "JSW Steel",
    "JSWENERGY": "JSW Energy",
    "MOTHERSON": "Samvardhana Motherson",
    # Abbreviations with a more famous owner elsewhere, or ordinary words.
    "BEL": "Bharat Electronics",
    "LT": "Larsen & Toubro",
    "M&M": "Mahindra & Mahindra",
    "ITC": "ITC Limited",
    "SBIN": "State Bank of India",
    "ONGC": "Oil and Natural Gas Corporation",
    "NTPC": "NTPC Limited",
    "BPCL": "Bharat Petroleum",
    "IOC": "Indian Oil Corporation",
    "GAIL": "GAIL India",
    "PNB": "Punjab National Bank",
    "DLF": "DLF Limited",
    "VEDL": "Vedanta Limited",
    "IRFC": "Indian Railway Finance Corporation",
    "DMART": "Avenue Supermarts DMart",
    "INFY": "Infosys",
    "HINDUNILVR": "Hindustan Unilever",
    "BHARTIARTL": "Bharti Airtel",
    "MARUTI": "Maruti Suzuki",
    "SUNPHARMA": "Sun Pharmaceutical",
    "ULTRACEMCO": "UltraTech Cement",
    "POWERGRID": "Power Grid Corporation",
    "COALINDIA": "Coal India",
    "HINDALCO": "Hindalco Industries",
    "GRASIM": "Grasim Industries",
    "HCLTECH": "HCL Technologies",
    "TECHM": "Tech Mahindra",
    "NESTLEIND": "Nestle India",
    "ASIANPAINT": "Asian Paints",
    "DRREDDY": "Dr Reddy's Laboratories",
    "EICHERMOT": "Eicher Motors",
    "HEROMOTOCO": "Hero MotoCorp",
    "BRITANNIA": "Britannia Industries",
    "SHREECEM": "Shree Cement",
    "APOLLOHOSP": "Apollo Hospitals",
    "INDUSINDBK": "IndusInd Bank",
    "SBILIFE": "SBI Life Insurance",
    "HDFCLIFE": "HDFC Life Insurance",
    "DIVISLAB": "Divi's Laboratories",
    "BOSCHLTD": "Bosch India",
    "AMBUJACEM": "Ambuja Cements",
    "KOTAKBANK": "Kotak Mahindra Bank",
}


def query_for(symbol):
    """The Google News search string for one symbol."""
    name = COMPANY_NAMES.get(symbol)
    return f"{name} NSE share" if name else f"{symbol} NSE share price"


def match_terms(symbol):
    """Lowercased strings whose presence in a headline means it is about `symbol`.

    Used only for the market-wide feeds, where a headline has to be attributed
    rather than being about the company by construction.
    """
    terms = {symbol.lower()}
    name = COMPANY_NAMES.get(symbol)
    if name:
        # "Larsen & Toubro" also matches on "Larsen"; the ampersand form alone
        # would miss most headlines.
        terms.add(name.lower())
        first = name.split()[0].lower()
        if len(first) > 3:
            terms.add(first)
    return terms


# Headlines that are not news. Two kinds dominate a Google News query for a ticker:
# broker price-widget pages ("X Share Price Today, Live NSE Updates"), which are
# templates with a company name in them, and "stocks to watch" roundups that list
# a dozen unrelated names. Neither carries a view about the company, and both
# crowd out the ones that do — ten headlines is the budget, and spending it on
# templates means scoring nothing.
#
# Matched on the title only, case-insensitively. Deliberately narrow: a filter
# that guesses at relevance would be a second unvalidated model in front of the
# first one.
NOISE_PATTERNS = (
    r"share price today",
    r"stock price live",
    r"live nse/bse",
    r"share price -\s*live",
    r"stocks? to (watch|buy or sell)",
    r"^stocks in news",
    r"market wrap",
)


def is_noise(title):
    lowered = title.lower()
    return any(re.search(p, lowered) for p in NOISE_PATTERNS)


def _parse_rss(text, source):
    """(title, link, published, source) per item. Never raises on malformed XML."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []

    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "link": (item.findtext("link") or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
            "source": source,
        })
    return out


def _fetch(url, source):
    try:
        response = requests.get(
            url,
            timeout=FEED_TIMEOUT_SECONDS,
            headers={"User-Agent": "Mozilla/5.0 (compatible; nse-assist/1.0)"},
        )
    except requests.RequestException as exc:
        print(f"[news] {source} unreachable ({exc.__class__.__name__}) — skipping")
        return []
    if response.status_code >= 400:
        print(f"[news] {source} -> HTTP {response.status_code} — skipping")
        return []
    return _parse_rss(response.text, source)


def _parsed_date(raw):
    """RFC-822 to a date, or None. Feeds disagree about format constantly."""
    if not raw:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def _recent(items, as_of=None, max_age_days=MAX_HEADLINE_AGE_DAYS):
    """Drop items older than the window. Undated items are KEPT.

    Keeping the undated ones is the conservative choice here: a feed that omits
    pubDate would otherwise contribute nothing at all, and the alternative failure
    — an old headline sneaking in — costs an observational score that nothing acts
    on.
    """
    reference = as_of or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    out = []
    for item in items:
        stamp = _parsed_date(item.get("published"))
        if stamp is None:
            out.append(item)
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if (reference - stamp).days <= max_age_days:
            out.append(item)
    return out


def market_headlines():
    """One fetch per market feed, shared across every symbol in the run."""
    items = []
    for source, url in MARKET_FEEDS:
        items.extend(_fetch(url, source))
    return items


def filter_by_symbol(items, symbol):
    terms = match_terms(symbol)
    # Word-boundary matching: "IOC" must not match "associoc", and a bare substring
    # test on three-letter tickers matches a great deal of unrelated text.
    patterns = [re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE) for term in terms]
    return [i for i in items if any(p.search(i["title"]) for p in patterns)]


def headlines_for(symbol, market_items=None, limit=MAX_HEADLINES_PER_SYMBOL):
    """Recent headlines about one symbol, per-symbol search plus filtered market feeds.

    Returns [] on any failure. Deduplicated by title, because the same story
    reaches both sources routinely.
    """
    items = _fetch(GOOGLE_NEWS_RSS.format(query=quote_plus(query_for(symbol))),
                   f"google-news:{symbol}")
    if market_items:
        items.extend(filter_by_symbol(market_items, symbol))

    seen, unique = set(), []
    for item in _recent(items):
        if is_noise(item["title"]):
            continue
        key = item["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:limit]


def assert_consistent():
    """Guards the mapping edit that fails silently. Called by the doctor stage.

    A key that is not a universe symbol is inert rather than wrong — the symbol
    falls through to the generic query and nothing reports that its mapping was
    ignored. That is exactly the kind of defect that survives for months.
    """
    from src import universe

    unknown = sorted(set(COMPANY_NAMES) - set(universe.UNIVERSE))
    if unknown:
        raise RuntimeError(
            f"COMPANY_NAMES has {len(unknown)} key(s) not in the universe: "
            f"{', '.join(unknown[:6])}"
        )
    return f"{len(COMPANY_NAMES)} of {len(universe.UNIVERSE)} symbols name-mapped"
