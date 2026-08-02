"""The tradable universe: NIFTY 100 constituents, as a committed constant.

Committed rather than fetched on purpose. An index membership change should show
up as a reviewable diff — "these three symbols entered, these three left" — not as
a silent shift in what the scanner looked at last night. It also keeps ingest
deterministic and offline-testable.

Symbols are NSE trading symbols (the same strings NSE's own bhavcopy uses).
ingest.py appends the ".NS" suffix Yahoo needs; nothing else should.

SNAPSHOT DATE: 2026-08-02. NSE reconstitutes the index semi-annually (March and
September). To refresh, diff against the constituent list published at
https://www.niftyindices.com/indices/equity/broad-based-indices/nifty-100 and
update this tuple in a single commit.

Every symbol below was verified to resolve on the price feed on the snapshot date.
Three corrections came out of that check, and they are the kind that recur:

  TATAMOTORS  demerged into TMPV (passenger vehicles) and TMCV (commercial, which
              retains the "Tata Motors Limited" name). Both are carried here.
  UNITDSPTS   United Spirits trades as UNITDSPR.
  LTIM        LTIMindtree no longer resolves on the feed at all; dropped rather
              than carried as a symbol that 404s every morning.

So: when a symbol starts failing in ingest, suspect a corporate action before
suspecting the feed, and confirm against niftyindices rather than guessing.
"""

from datetime import date

# NIFTY 50.
NIFTY_50 = (
    "ADANIENT",
    "ADANIPORTS",
    "APOLLOHOSP",
    "ASIANPAINT",
    "AXISBANK",
    "BAJAJ-AUTO",
    "BAJAJFINSV",
    "BAJFINANCE",
    "BEL",
    "BHARTIARTL",
    "CIPLA",
    "COALINDIA",
    "DRREDDY",
    "EICHERMOT",
    "ETERNAL",
    "GRASIM",
    "HCLTECH",
    "HDFCBANK",
    "HDFCLIFE",
    "HEROMOTOCO",
    "HINDALCO",
    "HINDUNILVR",
    "ICICIBANK",
    "INDUSINDBK",
    "INFY",
    "ITC",
    "JIOFIN",
    "JSWSTEEL",
    "KOTAKBANK",
    "LT",
    "M&M",
    "MARUTI",
    "NESTLEIND",
    "NTPC",
    "ONGC",
    "POWERGRID",
    "RELIANCE",
    "SBILIFE",
    "SBIN",
    "SHRIRAMFIN",
    "SUNPHARMA",
    "TATACONSUM",
    "TATASTEEL",
    "TCS",
    "TECHM",
    "TITAN",
    "TMPV",
    "TRENT",
    "ULTRACEMCO",
    "WIPRO",
)

# The other half of the NIFTY 100 (broadly the NIFTY Next 50).
NIFTY_NEXT_50 = (
    "ABB",
    "ADANIENSOL",
    "ADANIGREEN",
    "ADANIPOWER",
    "AMBUJACEM",
    "BAJAJHLDNG",
    "BANKBARODA",
    "BOSCHLTD",
    "BPCL",
    "BRITANNIA",
    "CANBK",
    "CGPOWER",
    "CHOLAFIN",
    "DABUR",
    "DIVISLAB",
    "DLF",
    "DMART",
    "GAIL",
    "GODREJCP",
    "HAL",
    "HAVELLS",
    "HYUNDAI",
    "ICICIGI",
    "ICICIPRULI",
    "INDHOTEL",
    "INDIGO",
    "IOC",
    "IRFC",
    "JINDALSTEL",
    "JSWENERGY",
    "LICI",
    "LODHA",
    "MAZDOCK",
    "MOTHERSON",
    "NAUKRI",
    "PFC",
    "PIDILITIND",
    "PNB",
    "RECLTD",
    "SHREECEM",
    "SIEMENS",
    "SWIGGY",
    "TATAPOWER",
    "TMCV",
    "TORNTPHARM",
    "TVSMOTOR",
    "UNITDSPR",
    "VBL",
    "VEDL",
    "ZYDUSLIFE",
)

NIFTY_100 = NIFTY_50 + NIFTY_NEXT_50

# What the stages actually scan. Point this at NIFTY_50 to halve ingest time while
# tuning a rule, then widen it back — no other module needs to change.
UNIVERSE = NIFTY_100

# Yahoo's ticker for an NSE-listed equity is the NSE symbol plus this suffix.
YAHOO_SUFFIX = ".NS"


# When this list was last checked against niftyindices.com, and the months NSE
# reconstitutes in. A stale universe is the quietest failure in the repo: nothing
# errors, the scan simply looks at last season's index — missing whatever entered
# and carrying whatever left, both invisibly.
SNAPSHOT_DATE = date(2026, 8, 2)
RECONSTITUTION_MONTHS = (3, 9)   # March and September, semi-annual


def _reconstitutions_since(snapshot, today):
    """How many reconstitution months have started since the snapshot."""
    count = 0
    year = snapshot.year
    while year <= today.year:
        for month in RECONSTITUTION_MONTHS:
            event = date(year, month, 1)
            if snapshot < event <= today:
                count += 1
        year += 1
    return count


def snapshot_warning(today=None):
    """A sentence when a reconstitution has passed since the snapshot, else None."""
    today = today or date.today()
    missed = _reconstitutions_since(SNAPSHOT_DATE, today)
    if not missed:
        return None
    return (f"universe snapshot is {SNAPSHOT_DATE} and {missed} NIFTY 100 "
            f"reconstitution(s) have happened since — diff against "
            f"niftyindices.com and update src/universe.py")


def yahoo_ticker(symbol):
    return f"{symbol}{YAHOO_SUFFIX}"


def assert_consistent():
    """Guards the two things that quietly break a universe edit: a duplicate symbol
    (double-counted in every scan) and a count that drifted from 100."""
    duplicates = sorted({s for s in NIFTY_100 if NIFTY_100.count(s) > 1})
    if duplicates:
        raise RuntimeError(f"duplicate symbols in universe: {', '.join(duplicates)}")
    if len(NIFTY_100) != 100:
        raise RuntimeError(f"NIFTY_100 has {len(NIFTY_100)} symbols, expected 100")
    return f"{len(UNIVERSE)} symbols"
