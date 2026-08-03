"""Mutual-fund schemes tracked for short-horizon parking — a committed constant.

Committed for the same reason as the universe and the risk limits: which schemes
you park cash in is a decision, and a decision belongs in git history where the
change is reviewable, not in an environment variable that silently differs between
your laptop and the scheduled runner.

AMFI's dump carries ~17,000 schemes. Storing all of them is a megabyte a day
against a database that gets committed after every run, so this list is what makes
the funds stage cheap.

FINDING SCHEME CODES — do not hunt through the raw file:

    python main.py --stage funds --search "hdfc liquid"

That prints code, category and latest NAV for every match, which is the whole
input needed to add a line below.

A NOTE ON PLANS: a scheme's Direct and Regular plans have different codes and
different NAVs — Regular carries distributor commission, so it underperforms by
the trail fee. Growth and IDCW variants differ again. The codes below are all
Direct/Growth. Check the plan in the name before adding one.
"""

# Categories that make sense for parking cash between trades. AMFI's own naming is
# inconsistent across these headers — "Hybrid Scheme - Arbitrage Fund" and "Hybrid
# Schemes - Arbitrage Fund" both occur, as do "Ultra Short Duration Fund" and "Ultra
# Short Term Fund" — so these are matched as case-insensitive substrings rather than
# compared for equality.
PARKING_CATEGORIES = (
    "Liquid Fund",
    "Overnight Fund",
    "Money Market",
    "Ultra Short",
    "Low Duration",
    "Short Duration",
    "Arbitrage Fund",
)

# (scheme_code, label, category) — the label is yours, for the Telegram report;
# the category is a note to the reader, not something the code matches on.
#
# THESE FOUR ARE PLACEHOLDER DEFAULTS, not a recommendation. They are real, live
# Direct/Growth schemes from large houses, chosen so the stage does something
# useful out of the box. Replace them with the schemes you actually hold — use
# --search to find codes.
# TWO PER CATEGORY WHERE POSSIBLE, AND FROM DIFFERENT HOUSES.
#
# A category holding one scheme cannot be ranked: the digest scores it 0.50 and
# calls it 1 of 1, which describes nothing. Two is the minimum at which "steadier"
# and "jumpier" mean anything, and putting them in different fund houses keeps the
# comparison from measuring one AMC's treasury desk against itself.
#
# The second liquid and arbitrage schemes were also chosen for a property the
# incumbents lack: neither restates its unit face value. Both HDFC schemes here
# restate on 2015-08-30 (x100.04), so every window crossing that returns None and
# their long-run figures are withheld. The SBI and Kotak series are continuous
# from 2013, so the digest has at least one complete history to rank against in
# each of those categories.
WATCHLIST = (
    ("119091", "HDFC Liquid — Direct Growth", "Liquid Fund"),
    ("119800", "SBI Liquid — Direct Growth", "Liquid Fund"),
    ("120364", "ICICI Pru Arbitrage — Direct Growth", "Arbitrage Fund"),
    ("119771", "Kotak Arbitrage — Direct Growth", "Arbitrage Fund"),
    ("120676", "ICICI Pru Ultra Short Term — Direct Growth", "Ultra Short"),
    ("119092", "HDFC Money Market — Direct Growth", "Money Market"),
)

SCHEME_CODES = tuple(code for code, _, _ in WATCHLIST)
LABELS = {code: label for code, label, _ in WATCHLIST}
CATEGORIES = {code: category for code, _, category in WATCHLIST}


def label_for(code):
    return LABELS.get(str(code), str(code))


def is_parking_category(category):
    """True when an AMFI category header is one of the parking categories."""
    if not category:
        return False
    lowered = category.lower()
    return any(key.lower() in lowered for key in PARKING_CATEGORIES)


def assert_consistent():
    """Guards the edits that quietly break a watchlist: a duplicate code, or a code
    that is not a code. Called by the doctor stage."""
    codes = [code for code, _, _ in WATCHLIST]
    duplicates = sorted({c for c in codes if codes.count(c) > 1})
    if duplicates:
        raise RuntimeError(f"duplicate scheme codes: {', '.join(duplicates)}")
    bad = [c for c in codes if not str(c).isdigit()]
    if bad:
        raise RuntimeError(f"scheme codes must be numeric (AMFI codes, not ISINs): {', '.join(bad)}")
    if not codes:
        return "empty — the funds stage will skip"
    return f"{len(codes)} scheme(s) across {len({c for _, _, c in WATCHLIST})} categories"
