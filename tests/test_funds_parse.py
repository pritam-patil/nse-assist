"""parse_dump() against AMFI's actual on-the-wire shape — not a guess at it.

    python -m unittest discover -s tests -v

This file exists because the format DID change under us: AMFI split what used
to be free text baked into "Scheme Name" ("XYZ Fund - Direct Plan - Growth")
into three real columns (Name, Plan, Option) with no version marker anywhere
in the file, some time on or before 2026-08-19. Nothing here previously fed
parse_dump() a realistic sample, so the change went undetected until two
consecutive evening runs failed in production with "none of the 6 watchlist
schemes appear in today's dump" — three calls removed from the actual cause,
which was every row failing the field-count check and being silently dropped.

Every fixture below is a REAL line captured from a live AMFI fetch on
2026-08-19, not hand-authored — including SBI LIQUID FUND's row, which ships
with empty Plan and Option fields (still 8 semicolon-delimited fields, just
two of them blank). A hand-authored fixture would have "known" to fill those
in; the real file did not, and that is exactly the kind of edge a captured
sample catches and an assumed one hides.
"""

import unittest

from src import funds

HEADER = (
    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
    "Scheme Name;Plan;Option;Net Asset Value;Date"
)
CATEGORY_LINE = "Open Ended Schemes(Debt Scheme - Liquid Fund)"
HOUSE_LINE = "HDFC Mutual Fund"

# Captured verbatim from https://www.amfiindia.com/spages/NAVAll.txt, 2026-08-19.
HDFC_LIQUID_ROW = (
    "119091;INF179KB1HP9;-;HDFC Liquid Fund;Direct Plan;Growth Option;"
    "5554.2234;18-Aug-2026"
)
SBI_LIQUID_ROW_BLANK_PLAN = (
    "119800;INF200K01UT4;-;SBI LIQUID FUND;;;4422.0424;18-Aug-2026"
)
SUSPENDED_ROW = (
    "999999;INF000K01ZZ9;-;Some Wound Up Fund;Direct Plan;Growth;"
    "0.0000;18-Aug-2026"
)


def dump(*lines):
    return "\n".join((HEADER, "", CATEGORY_LINE, "", HOUSE_LINE, "") + lines)


class FieldShapeTests(unittest.TestCase):
    """Pins the current 8-field contract itself — the test that would have
    caught this the day AMFI changed it, had it existed then."""

    def test_a_real_data_row_has_eight_fields(self):
        fields = [f.strip() for f in HDFC_LIQUID_ROW.split(";")]
        self.assertEqual(len(fields), 8)
        self.assertEqual(len(fields), funds.EXPECTED_FIELDS)

    def test_the_header_declares_the_same_eight_columns(self):
        fields = [f.strip() for f in HEADER.split(";")]
        self.assertEqual(len(fields), 8)


class ParseDumpTests(unittest.TestCase):
    def test_a_real_row_parses_with_nav_and_date_at_the_right_positions(self):
        rows = funds.parse_dump(dump(HDFC_LIQUID_ROW))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["scheme_code"], "119091")
        self.assertEqual(row["nav"], 5554.2234)
        self.assertEqual(row["date"], "2026-08-18")

    def test_name_is_reassembled_from_the_three_split_columns(self):
        rows = funds.parse_dump(dump(HDFC_LIQUID_ROW))
        self.assertEqual(rows[0]["name"], "HDFC Liquid Fund Direct Plan Growth Option")

    def test_a_blank_plan_and_option_does_not_leave_trailing_spaces(self):
        rows = funds.parse_dump(dump(SBI_LIQUID_ROW_BLANK_PLAN))
        self.assertEqual(rows[0]["name"], "SBI LIQUID FUND")

    def test_category_and_house_headers_attach_to_the_rows_that_follow(self):
        rows = funds.parse_dump(dump(HDFC_LIQUID_ROW))
        self.assertEqual(rows[0]["category"], CATEGORY_LINE)
        self.assertEqual(rows[0]["house"], HOUSE_LINE)

    def test_a_zero_nav_suspended_scheme_is_dropped(self):
        rows = funds.parse_dump(dump(SUSPENDED_ROW))
        self.assertEqual(rows, [])

    def test_scheme_codes_filters_to_the_requested_set(self):
        rows = funds.parse_dump(dump(HDFC_LIQUID_ROW, SBI_LIQUID_ROW_BLANK_PLAN),
                                scheme_codes=["119091"])
        self.assertEqual([r["scheme_code"] for r in rows], ["119091"])

    def test_the_header_row_itself_never_becomes_a_scheme(self):
        # "Scheme Code" in field 0 fails the .isdigit() guard, same as before.
        rows = funds.parse_dump(dump())
        self.assertEqual(rows, [])

    def test_a_row_with_the_pre_split_six_field_shape_no_longer_matches(self):
        # This is the regression itself, made explicit: the OLD format (name
        # with plan/option baked in, no separate columns) now correctly fails
        # the field-count check rather than silently misreading NAV as a date
        # or similar. If AMFI ever reverts, this test is the one that goes red
        # and explains why.
        old_shape_row = (
            "119091;INF179KB1HP9;-;HDFC Liquid Fund - Direct Plan - Growth;"
            "5554.2234;18-Aug-2026"
        )
        self.assertEqual(len(old_shape_row.split(";")), 6)
        rows = funds.parse_dump(dump(old_shape_row))
        self.assertEqual(rows, [])


class WatchlistRegressionTests(unittest.TestCase):
    """The exact production failure, reproduced from a real multi-scheme
    sample: every watchlist code must survive the parse."""

    ALL_SIX_ROWS = (
        "119091;INF179KB1HP9;-;HDFC Liquid Fund;Direct Plan;Growth Option;"
        "5554.2234;18-Aug-2026\n"
        "119800;INF200K01UT4;-;SBI LIQUID FUND;;;4422.0424;18-Aug-2026\n"
        "120364;INF109K016O4;-;ICICI Prudential Arbitrage Fund;Direct Plan;"
        "Growth;39.5612;19-Aug-2026\n"
        "119771;INF174K01LC6;-;Kotak Arbitrage Fund;Direct Plan;Growth;"
        "43.1109;19-Aug-2026\n"
        "120676;INF109K01T04;-;ICICI Prudential Ultra Short term Fund;"
        "Direct Plan;Growth;32.2772;19-Aug-2026\n"
        "119092;INF179KB1HU9;-;HDFC Money Market Fund;Direct Plan;"
        "Growth Option;6276.6101;18-Aug-2026"
    )

    def test_all_six_watchlist_schemes_survive_the_parse(self):
        from src import fund_watchlist
        rows = funds.parse_dump(dump(self.ALL_SIX_ROWS), fund_watchlist.SCHEME_CODES)
        found = {r["scheme_code"] for r in rows}
        self.assertEqual(found, set(fund_watchlist.SCHEME_CODES))


if __name__ == "__main__":
    unittest.main()
