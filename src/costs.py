"""Transaction costs for NSE equities — committed, like the risk parameters.

Rates are a decision about which broker and product you use, not a credential, so
they belong in git where a change is reviewable and a backtest can be re-run
against the rates that were live at the time.

    >>> python main.py --stage doctor      # prints a worked example

THESE ARE TYPICAL DISCOUNT-BROKER RATES (Zerodha/Groww-style), current as of the
2026 snapshot below. Statutory charges change in most Union Budgets and brokers
change their own fees whenever they like. Verify against your contract note before
trusting a net number — a wrong rate here does not fail loudly, it just quietly
shifts every expectancy figure.

Two things about Indian equities drive the whole model:

DELIVERY vs INTRADAY are different products with different taxes. Delivery pays STT
on both legs (0.1% each) and a flat per-scrip DP fee on the sell; intraday pays STT
on the sell only (0.025%) but pays brokerage. Holding overnight makes it delivery.

A SHORT CANNOT BE DELIVERY. You cannot deliver shares you do not own, so a short in
the cash segment must be squared off the same session. Holding a short overnight is
not expensive, it is *not possible* — it would need the F&O segment, where one lot
of a NIFTY 100 name is several lakh of notional and cannot be sized to a 25,000
position at all. short_is_executable() exists so a backtest cannot quietly price
something the market will not let you do.
"""

RATES_SNAPSHOT = "2026-08"

# Per executed order. Discount brokers charge nothing for delivery and the lower of
# a flat fee or a percentage for intraday.
BROKERAGE_DELIVERY = 0.0
BROKERAGE_INTRADAY_FLAT = 20.0
BROKERAGE_INTRADAY_PCT = 0.0003  # 0.03%

# Securities Transaction Tax. The single largest cost on a delivery round trip.
STT_DELIVERY_BUY = 0.001  # 0.1%
STT_DELIVERY_SELL = 0.001
STT_INTRADAY_SELL = 0.00025  # 0.025%, sell side only

# NSE transaction charge, both legs, both products.
EXCHANGE_TXN = 0.0000297  # 0.00297%

# SEBI turnover fee: ₹10 per crore.
SEBI_TURNOVER = 0.000001

# Stamp duty, buy side only.
STAMP_DELIVERY = 0.00015  # 0.015%
STAMP_INTRADAY = 0.00003  # 0.003%

# GST on the *service* components only — brokerage, exchange charges, SEBI fee.
# Not on STT or stamp duty, which are taxes rather than services.
GST = 0.18

# Depository charge on a delivery SELL: flat, per scrip, regardless of quantity.
# Flat is what makes it bite — 15.93 on a 25,000 position is 0.064%, and on a
# 5,000 position it would be 0.32%. It is a large part of why small delivery
# positions are inefficient.
DP_CHARGE_PER_SELL = 15.93  # ₹13.50 + 18% GST

DELIVERY = "delivery"
INTRADAY = "intraday"


def short_is_executable(holding_days, segment=None):
    """False for an overnight short in the cash segment — which is not a cost
    question but a legality one. See the module docstring."""
    return holding_days == 0


def _brokerage(turnover, segment):
    if segment == DELIVERY:
        return BROKERAGE_DELIVERY
    return min(BROKERAGE_INTRADAY_FLAT, turnover * BROKERAGE_INTRADAY_PCT)


def round_trip(entry_price, exit_price, size, segment=DELIVERY):
    """Every charge on one complete round trip, itemised. All amounts in INR.

    Both legs are priced on their own turnover rather than on an average, because
    STT and stamp duty apply to buy and sell at different rates.
    """
    if size <= 0:
        # Every key the populated path returns. A partial dict here would blow up
        # any caller that formats the full shape — the same trap summarize() had.
        return {"brokerage": 0.0, "stt": 0.0, "exchange": 0.0, "sebi": 0.0, "stamp": 0.0,
                "gst": 0.0, "dp": 0.0, "total": 0.0, "turnover": 0.0, "breakeven_pct": 0.0}

    buy_turnover = entry_price * size
    sell_turnover = exit_price * size
    turnover = buy_turnover + sell_turnover

    brokerage = _brokerage(buy_turnover, segment) + _brokerage(sell_turnover, segment)

    if segment == DELIVERY:
        stt = buy_turnover * STT_DELIVERY_BUY + sell_turnover * STT_DELIVERY_SELL
        stamp = buy_turnover * STAMP_DELIVERY
        dp = DP_CHARGE_PER_SELL
    else:
        stt = sell_turnover * STT_INTRADAY_SELL
        stamp = buy_turnover * STAMP_INTRADAY
        dp = 0.0

    exchange = turnover * EXCHANGE_TXN
    sebi = turnover * SEBI_TURNOVER
    gst = (brokerage + exchange + sebi) * GST

    total = brokerage + stt + exchange + sebi + stamp + gst + dp
    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange": round(exchange, 2),
        "sebi": round(sebi, 2),
        "stamp": round(stamp, 2),
        "gst": round(gst, 2),
        "dp": round(dp, 2),
        "total": round(total, 2),
        "turnover": round(turnover, 2),
        # What the position must gain just to break even, as a fraction of the
        # entry notional. The number to compare an expectancy against.
        "breakeven_pct": round(total / buy_turnover, 5) if buy_turnover else 0.0,
    }


def segment_for(holding_days):
    """Delivery once a position is held overnight, intraday otherwise."""
    return INTRADAY if holding_days == 0 else DELIVERY


def as_dict():
    return {
        "snapshot": RATES_SNAPSHOT,
        "brokerage_delivery": BROKERAGE_DELIVERY,
        "brokerage_intraday": f"min({BROKERAGE_INTRADAY_FLAT}, {BROKERAGE_INTRADAY_PCT:.2%})",
        "stt_delivery": f"{STT_DELIVERY_BUY:.3%} buy + {STT_DELIVERY_SELL:.3%} sell",
        "stt_intraday": f"{STT_INTRADAY_SELL:.3%} sell",
        "stamp_delivery": f"{STAMP_DELIVERY:.3%} buy",
        "gst": f"{GST:.0%} on services",
        "dp_per_sell": DP_CHARGE_PER_SELL,
    }


def describe_example(notional=25_000):
    """A worked round trip at a flat price, for the doctor and backtest stages.

    Priced at a notional-appropriate share count rather than a fixed one, so the
    flat DP fee lands at its real proportion — that fee is the whole reason the
    break-even percentage moves with position size.
    """
    price = 1000.0
    size = max(1, int(notional // price))
    out = round_trip(price, price, size, DELIVERY)
    return (
        f"delivery round trip on {notional:,} notional costs {out['total']:,.0f} "
        f"({out['breakeven_pct']:.2%} to break even); STT {out['stt']:.0f}, DP {out['dp']:.0f}"
    )
