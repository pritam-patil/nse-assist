"""What a round trip actually costs — every friction from params.yaml, itemized.

    from data import frictions
    cfg = frictions.Config.from_params()
    result = frictions.trade(cfg, quantity=100, buy_price=300.0, sell_price=280.0,
                             buy_date=date(2025, 7, 30), sell_date=date(2025, 8, 20),
                             dividend_per_share=12.0, record_date=date(2025, 8, 6))

Nothing here is hard-coded: brokerage, STT, stamp duty, exchange and SEBI
charges, GST, the DP charge, slippage, the dividend slab rate, the STCG rate and
the Section 94(7) switch all come from params.yaml, so burst 6's sensitivity
runs are edits to one file, not to code.

MODELING DECISIONS, WRITTEN DOWN BEFORE THE BACKTEST WANTS THEM VAGUE

  Slippage lives in the executed price, not in the charge list. A buy fills
  slippage_bps above the quoted price, a sell below, and every turnover-based
  charge (STT, stamp, exchange, SEBI) is computed on those executed values —
  slippage compounds into the statutory costs exactly as it does at a broker.

  GST applies to brokerage + exchange txn + SEBI + DP. params.yaml says
  "brokerage + txn charges + DP"; a contract note groups exchange and SEBI
  levies together as transaction charges, and that is the reading here. STT and
  stamp duty are taxes, not services, and carry no GST.

  capital_pnl is all-in: executed price difference minus every charge. The
  Income-tax Act computes the capital gain slightly differently (STT is not a
  deductible cost of acquisition), which shifts the tax by stcg x STT — about
  four hundredths of a percent of turnover. Accepted as a simplification and
  noted here so nobody rediscovers it as a bug.

  A capital loss is valued as an offset: allowed loss x STCG rate, on the
  assumption there are other short-term gains to absorb it in the same year.
  That is the most favourable legal reading, which makes it the CONSERVATIVE
  choice for judging a dividend-capture idea — 94(7)'s bite is measured against
  the best case, not a strawman.

  TDS (tds_threshold_inr) is withholding — cash timing, not cost — and does not
  appear in net(). The slab tax on the dividend is the cost and is charged in
  full.

SECTION 94(7), THE RULE THIS MODULE EXISTS TO PRICE

Buy within three months BEFORE the record date, sell within three months AFTER,
and any capital loss is disallowed up to the dividend received. Both windows are
calendar months, boundaries inclusive. The disallowed slice earns no offset
value; what remains of the loss still does. Every dividend-capture trade sits
squarely inside both windows, which is why this clause gets a dedicated model
and a hand-computed test rather than a footnote.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

PARAMS_PATH = Path(__file__).resolve().parent.parent / "params.yaml"

SECTION_94_7_MONTHS = 3


def add_months(day, months):
    """Calendar-month arithmetic with day-of-month clamping (May 31 - 3 months
    is Feb 28/29, not an exception)."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = (date(year + month // 12, month % 12 + 1, 1)
                - date(year, month, 1)).days
    return date(year, month, min(day.day, last_day))


@dataclass(frozen=True)
class Config:
    """params.yaml's frictions and tax blocks, as fractions and rupees.

    Percentages become fractions HERE, once — every formula downstream multiplies
    a rate, and a stray /100 in one formula but not another is the classic way a
    cost model goes quietly wrong by two orders of magnitude.
    """
    brokerage_buy: float        # flat rupees per order
    brokerage_sell: float
    stt_rate: float             # of turnover, both sides
    stamp_rate: float           # buy side only
    exchange_rate: float        # both sides
    sebi_rate: float            # both sides
    gst_rate: float             # on brokerage + txn charges + DP
    dp_per_sell: float          # flat rupees, per scrip per sell day
    slippage_bps: float
    dividend_slab_rate: float
    stcg_rate: float
    apply_section_94_7: bool

    @classmethod
    def from_params(cls, path=None):
        params = yaml.safe_load(Path(path or PARAMS_PATH).read_text())
        frictions = params["frictions"]
        tax = params["tax"]
        return cls(
            brokerage_buy=float(frictions["brokerage"]["delivery_buy_inr"]),
            brokerage_sell=float(frictions["brokerage"]["delivery_sell_inr"]),
            stt_rate=float(frictions["stt_delivery_pct"]) / 100,
            stamp_rate=float(frictions["stamp_duty_buy_pct"]) / 100,
            exchange_rate=float(frictions["exchange_txn_pct"]) / 100,
            sebi_rate=float(frictions["sebi_fee_pct"]) / 100,
            gst_rate=float(frictions["gst_pct"]) / 100,
            dp_per_sell=float(frictions["dp_charge_per_sell_inr"]),
            slippage_bps=float(frictions["slippage_bps"]),
            dividend_slab_rate=float(tax["dividend_slab_pct"]) / 100,
            stcg_rate=float(tax["stcg_pct"]) / 100,
            apply_section_94_7=bool(tax["apply_section_94_7"]),
        )


def section_94_7_applies(cfg, buy_date, sell_date, record_date, dividend_gross):
    """Both statutory windows, boundaries inclusive, and a dividend actually
    received. Any missing date means the question cannot be asked — and the
    answer is then False, never a guess."""
    if not cfg.apply_section_94_7 or dividend_gross <= 0:
        return False
    if buy_date is None or sell_date is None or record_date is None:
        return False
    bought_inside = add_months(record_date, -SECTION_94_7_MONTHS) <= buy_date <= record_date
    sold_inside = record_date <= sell_date <= add_months(record_date, SECTION_94_7_MONTHS)
    return bought_inside and sold_inside


def trade(cfg, quantity, buy_price, sell_price, buy_date=None, sell_date=None,
          dividend_per_share=0.0, record_date=None):
    """One settled round trip, everything itemized, nothing netted silently.

    Returns a dict whose parts reconcile exactly:
      net = capital_pnl - capital_tax + dividend_gross - dividend_tax
    with capital_tax negative when a loss earns offset value. All rupee amounts
    are rounded to the paise at the edge; arithmetic runs at full precision.
    """
    slip = cfg.slippage_bps / 10_000
    buy_exec = buy_price * (1 + slip)
    sell_exec = sell_price * (1 - slip)
    buy_turnover = buy_exec * quantity
    sell_turnover = sell_exec * quantity
    both = buy_turnover + sell_turnover

    brokerage = cfg.brokerage_buy + cfg.brokerage_sell
    stt = cfg.stt_rate * both
    stamp_duty = cfg.stamp_rate * buy_turnover
    exchange_txn = cfg.exchange_rate * both
    sebi_fee = cfg.sebi_rate * both
    dp_charge = cfg.dp_per_sell
    gst = cfg.gst_rate * (brokerage + exchange_txn + sebi_fee + dp_charge)
    charges = brokerage + stt + stamp_duty + exchange_txn + sebi_fee + dp_charge + gst

    capital_pnl = (sell_exec - buy_exec) * quantity - charges
    dividend_gross = dividend_per_share * quantity
    dividend_tax = cfg.dividend_slab_rate * dividend_gross

    applies = section_94_7_applies(cfg, buy_date, sell_date, record_date, dividend_gross)
    if capital_pnl >= 0:
        disallowed_loss = 0.0
        capital_tax = cfg.stcg_rate * capital_pnl
    else:
        loss = -capital_pnl
        disallowed_loss = min(loss, dividend_gross) if applies else 0.0
        # The allowed remainder still shelters other gains; the disallowed slice
        # is simply worth nothing. Negative tax = the offset's value.
        capital_tax = -cfg.stcg_rate * (loss - disallowed_loss)

    net = capital_pnl - capital_tax + dividend_gross - dividend_tax

    money = {
        "buy_exec": buy_exec, "sell_exec": sell_exec,
        "brokerage": brokerage, "stt": stt, "stamp_duty": stamp_duty,
        "exchange_txn": exchange_txn, "sebi_fee": sebi_fee,
        "dp_charge": dp_charge, "gst": gst, "charges": charges,
        "capital_pnl": capital_pnl, "capital_tax": capital_tax,
        "dividend_gross": dividend_gross, "dividend_tax": dividend_tax,
        "disallowed_loss": disallowed_loss, "net": net,
    }
    return {"section_94_7_applied": applies,
            **{name: round(value, 2) for name, value in money.items()}}
