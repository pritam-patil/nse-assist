"""The friction model: three hand-computed round trips, and 94(7)'s boundaries.

    python -m unittest discover -s tests -v

The three trades in HandComputedTrades were worked by hand, every line of
arithmetic in the comments, before the module produced its numbers. If the
implementation and the comments disagree, believe the comments, then find out
which of the two is wrong — that is the entire value of hand-computed cases.
"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from data import frictions
from data.frictions import Config


# Round numbers chosen for hand arithmetic, not realism; realism lives in
# params.yaml and is exercised by the parsing tests.
CFG = Config(
    brokerage_buy=20.0, brokerage_sell=20.0,
    stt_rate=0.001, stamp_rate=0.00015,
    exchange_rate=0.0000297, sebi_rate=0.000001,
    gst_rate=0.18, dp_per_sell=15.0, slippage_bps=0.0,
    dividend_slab_rate=0.312, stcg_rate=0.20, apply_section_94_7=True,
)


class HandComputedTrades(unittest.TestCase):
    def test_trade_one_a_plain_winner_no_dividend(self):
        # 100 shares, buy 100, sell 110, no slippage.
        #   turnover: 10,000 buy + 11,000 sell = 21,000
        #   brokerage 20+20            = 40
        #   stt      0.001 x 21,000    = 21
        #   stamp    0.00015 x 10,000  = 1.50
        #   exchange 0.0000297 x 21,000 = 0.6237
        #   sebi     0.000001 x 21,000  = 0.021
        #   dp                          = 15
        #   gst 0.18 x (40 + 0.6237 + 0.021 + 15) = 10.016046
        #   charges                     = 88.160746
        #   capital_pnl = 1,000 - 88.160746        = 911.84
        #   capital_tax = 0.20 x 911.839254        = 182.37
        #   net = 911.839254 - 182.3678508         = 729.47
        result = frictions.trade(CFG, quantity=100, buy_price=100.0, sell_price=110.0)
        self.assertEqual(result["brokerage"], 40.0)
        self.assertEqual(result["stt"], 21.0)
        self.assertEqual(result["stamp_duty"], 1.5)
        self.assertAlmostEqual(result["gst"], 10.02)
        self.assertAlmostEqual(result["charges"], 88.16)
        self.assertAlmostEqual(result["capital_pnl"], 911.84)
        self.assertAlmostEqual(result["capital_tax"], 182.37)
        self.assertAlmostEqual(result["net"], 729.47)
        self.assertFalse(result["section_94_7_applied"])
        self.assertEqual(result["disallowed_loss"], 0.0)

    def test_trade_two_a_dividend_capture_outside_the_94_7_window(self):
        # 50 shares, buy 200 with 10 bps slippage -> 200.20; sell 199 -> 198.801.
        # Bought four months before the record date, so 94(7) cannot apply.
        #   turnover: 10,010.00 buy + 9,940.05 sell = 19,950.05
        #   brokerage                    = 40
        #   stt      0.001 x 19,950.05   = 19.95005
        #   stamp    0.00015 x 10,010    = 1.5015
        #   exchange 0.0000297 x 19,950.05 = 0.59251649
        #   sebi     0.000001 x 19,950.05  = 0.01995005
        #   dp                            = 15
        #   gst 0.18 x (40 + 0.59251649 + 0.01995005 + 15) = 10.01024398
        #   charges                       = 87.07426051
        #   price pnl (198.801 - 200.20) x 50 = -69.95
        #   capital_pnl = -69.95 - 87.07426051  = -157.02
        #   dividend: 5 x 50 = 250 gross; slab 31.2% = 78 tax
        #   allowed loss shield = 0.20 x 157.02426051 = 31.40 (capital_tax -31.40)
        #   net = -157.02426051 + 31.40485210 + 250 - 78 = 46.38
        slipped = Config(**{**CFG.__dict__, "slippage_bps": 10.0})
        result = frictions.trade(
            slipped, quantity=50, buy_price=200.0, sell_price=199.0,
            buy_date=date(2025, 4, 1), sell_date=date(2025, 8, 20),
            dividend_per_share=5.0, record_date=date(2025, 8, 6))
        self.assertAlmostEqual(result["buy_exec"], 200.20)
        self.assertAlmostEqual(result["sell_exec"], 198.80)
        self.assertAlmostEqual(result["stt"], 19.95)
        self.assertAlmostEqual(result["charges"], 87.07)
        self.assertAlmostEqual(result["capital_pnl"], -157.02)
        self.assertFalse(result["section_94_7_applied"])
        self.assertEqual(result["disallowed_loss"], 0.0)
        self.assertAlmostEqual(result["capital_tax"], -31.40)
        self.assertAlmostEqual(result["dividend_tax"], 78.0)
        self.assertAlmostEqual(result["net"], 46.38)

    def test_trade_three_where_94_7_bites(self):
        # 100 shares, buy 300 a week before the record date, sell 280 two weeks
        # after, dividend 12 — squarely inside both windows.
        #   turnover: 30,000 buy + 28,000 sell = 58,000
        #   brokerage                   = 40
        #   stt      0.001 x 58,000     = 58
        #   stamp    0.00015 x 30,000   = 4.50
        #   exchange 0.0000297 x 58,000 = 1.7226
        #   sebi     0.000001 x 58,000  = 0.058
        #   dp                          = 15
        #   gst 0.18 x (40 + 1.7226 + 0.058 + 15) = 10.220508
        #   charges                     = 129.501108
        #   capital_pnl = -2,000 - 129.501108 = -2,129.50
        #   dividend: 1,200 gross; slab tax 374.40
        #   94(7): disallowed = min(2,129.50, 1,200) = 1,200
        #   allowed loss 929.501108 -> shield 185.90 (capital_tax -185.90)
        #   net = -2,129.501108 + 185.9002216 + 1,200 - 374.40 = -1,118.00
        result = frictions.trade(
            CFG, quantity=100, buy_price=300.0, sell_price=280.0,
            buy_date=date(2025, 7, 30), sell_date=date(2025, 8, 20),
            dividend_per_share=12.0, record_date=date(2025, 8, 6))
        self.assertTrue(result["section_94_7_applied"])
        self.assertAlmostEqual(result["charges"], 129.50)
        self.assertAlmostEqual(result["capital_pnl"], -2129.50)
        self.assertEqual(result["disallowed_loss"], 1200.0)
        self.assertAlmostEqual(result["capital_tax"], -185.90)
        self.assertAlmostEqual(result["dividend_tax"], 374.40)
        self.assertAlmostEqual(result["net"], -1118.00)

    def test_the_price_of_94_7_is_exactly_stcg_times_the_disallowed_loss(self):
        # Same trade as above with the clause switched off: the nets differ by
        # 0.20 x 1,200 = 240 — the clause's cost, isolated.
        kwargs = dict(quantity=100, buy_price=300.0, sell_price=280.0,
                      buy_date=date(2025, 7, 30), sell_date=date(2025, 8, 20),
                      dividend_per_share=12.0, record_date=date(2025, 8, 6))
        with_clause = frictions.trade(CFG, **kwargs)
        without = frictions.trade(
            Config(**{**CFG.__dict__, "apply_section_94_7": False}), **kwargs)
        self.assertFalse(without["section_94_7_applied"])
        self.assertAlmostEqual(without["net"] - with_clause["net"], 240.0)

    def test_the_parts_reconcile_to_the_net(self):
        result = frictions.trade(
            CFG, quantity=100, buy_price=300.0, sell_price=280.0,
            buy_date=date(2025, 7, 30), sell_date=date(2025, 8, 20),
            dividend_per_share=12.0, record_date=date(2025, 8, 6))
        recomposed = (result["capital_pnl"] - result["capital_tax"]
                      + result["dividend_gross"] - result["dividend_tax"])
        self.assertAlmostEqual(result["net"], recomposed, places=1)


class WindowTests(unittest.TestCase):
    RECORD = date(2025, 8, 6)

    def _applies(self, buy, sell):
        return frictions.section_94_7_applies(CFG, buy, sell, self.RECORD, 100.0)

    def test_both_boundaries_are_inclusive(self):
        self.assertTrue(self._applies(date(2025, 5, 6), date(2025, 11, 6)))

    def test_a_day_outside_either_window_escapes(self):
        self.assertFalse(self._applies(date(2025, 5, 5), date(2025, 8, 20)))
        self.assertFalse(self._applies(date(2025, 7, 30), date(2025, 11, 7)))

    def test_no_dividend_means_no_question(self):
        self.assertFalse(frictions.section_94_7_applies(
            CFG, date(2025, 7, 30), date(2025, 8, 20), self.RECORD, 0.0))

    def test_the_configured_switch_disables_the_clause(self):
        off = Config(**{**CFG.__dict__, "apply_section_94_7": False})
        self.assertFalse(frictions.section_94_7_applies(
            off, date(2025, 7, 30), date(2025, 8, 20), self.RECORD, 100.0))

    def test_a_profitable_trade_has_nothing_to_disallow(self):
        result = frictions.trade(
            CFG, quantity=100, buy_price=280.0, sell_price=300.0,
            buy_date=date(2025, 7, 30), sell_date=date(2025, 8, 20),
            dividend_per_share=12.0, record_date=date(2025, 8, 6))
        self.assertTrue(result["section_94_7_applied"])
        self.assertEqual(result["disallowed_loss"], 0.0)
        self.assertGreater(result["capital_tax"], 0)


class AddMonthsTests(unittest.TestCase):
    def test_day_of_month_clamps_at_february(self):
        self.assertEqual(frictions.add_months(date(2026, 5, 31), -3), date(2026, 2, 28))

    def test_year_rollover(self):
        self.assertEqual(frictions.add_months(date(2026, 11, 15), 3), date(2027, 2, 15))

    def test_a_plain_month_needs_no_clamp(self):
        self.assertEqual(frictions.add_months(date(2025, 8, 6), -3), date(2025, 5, 6))


class ConfigTests(unittest.TestCase):
    def test_percentages_become_fractions_exactly_once(self):
        text = (
            "frictions:\n"
            "  brokerage: {delivery_buy_inr: 9, delivery_sell_inr: 7}\n"
            "  stt_delivery_pct: 0.1\n"
            "  stamp_duty_buy_pct: 0.015\n"
            "  exchange_txn_pct: 0.00297\n"
            "  sebi_fee_pct: 0.0001\n"
            "  gst_pct: 18\n"
            "  dp_charge_per_sell_inr: 13\n"
            "  slippage_bps: 10\n"
            "tax:\n"
            "  dividend_slab_pct: 31.2\n"
            "  stcg_pct: 20\n"
            "  tds_threshold_inr: 10000\n"
            "  apply_section_94_7: true\n")
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(text)
            path = Path(handle.name)
        try:
            cfg = Config.from_params(path)
        finally:
            path.unlink()
        self.assertEqual(cfg.brokerage_buy, 9.0)
        self.assertEqual(cfg.brokerage_sell, 7.0)
        self.assertAlmostEqual(cfg.stt_rate, 0.001)
        self.assertAlmostEqual(cfg.stamp_rate, 0.00015)
        self.assertAlmostEqual(cfg.gst_rate, 0.18)
        self.assertEqual(cfg.dp_per_sell, 13.0)
        self.assertEqual(cfg.slippage_bps, 10.0)   # bps stay bps
        self.assertAlmostEqual(cfg.dividend_slab_rate, 0.312)
        self.assertAlmostEqual(cfg.stcg_rate, 0.20)
        self.assertTrue(cfg.apply_section_94_7)

    def test_the_repo_params_file_loads_and_is_sane(self):
        cfg = Config.from_params()
        self.assertTrue(0 < cfg.stt_rate < 0.01)
        self.assertTrue(0 < cfg.dividend_slab_rate < 0.45)
        self.assertTrue(0 < cfg.stcg_rate < 0.40)
        self.assertIsInstance(cfg.apply_section_94_7, bool)


if __name__ == "__main__":
    unittest.main()
