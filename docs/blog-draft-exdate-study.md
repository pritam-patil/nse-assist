# How much of a dividend does the price actually give up? A negative result from the NSE

*Draft for publication. Figures referenced as repo paths need re-hosting.
Status: personal research write-up; see the note at the end.*

Textbook finance says a stock should open ex-dividend lower by exactly the
dividend. Tax-clientele theory says the drop should be smaller — if the
marginal holder pays more tax on dividends than on capital gains, they'll pay
less for the right to receive one. The gap between those two claims is, in
principle, money. This is a write-up of measuring that gap on Indian large-cap
data and then following the arithmetic all the way to a verdict, which turns
out to be negative — twice, the second time in a way I found more instructive
than the first. Negative results in this genre rarely get written up, which is
precisely why the file drawers stay full of people re-running the same
backtest.

## Data and method

- **Universe and window**: the NIFTY 500 constituents, ten years of daily
  bars and dividend events (2016–2026) from a public EOD source — roughly
  5,000 dividend events after cleaning.
- **Adjustment basis, verified not assumed**: the source split-adjusts
  prices, volumes, *and* dividend amounts to the current share basis. I
  verified this against a large-cap's 2022 1:10 split rather than trusting
  documentation — my first pass assumed raw prices and silently understated
  every pre-split yield by the split factor. If you take one engineering
  lesson from this post: pin a handful of hand-verified events (I used three
  payouts transcribed from public announcements) and make the pipeline
  refuse to run when it disagrees with them.
- **The drop measurement**: close before ex-date to close on ex-date,
  adjusted for the index's same-day move (beta 1), expressed as a fraction
  of the dividend. Close-to-close is what EOD data honestly supports; it
  includes a session of drift the open-gap does not.
- **A statistical trap this design has to respect**: the ratio's denominator
  is the dividend, so measurement noise scales as 1/yield. A 0.1%-yield
  event carries ~15× the noise of a 1.5% one. Everything below is medians
  and yield buckets — on this data, a naive mean over all events is mostly
  noise.

## Finding 1: the price gives up about 87% of the dividend

| subset | n | median drop ratio | IQR |
|---|---|---|---|
| yield ≥ 2% | 557 | 0.89 | 0.55 – 1.19 |
| yield ≥ 1% | 1,420 | 0.87 | 0.32 – 1.31 |

Across every yield bucket where the noise permits measurement at all, the
median sits in a tight 0.85–0.97 band, with the IQR narrowing exactly as
1/yield predicts. So roughly 13% of the dividend is "left on the table"
pre-tax — consistent with the tax-clientele literature. Whether that gap is
*collectible* is a question about frictions, not about averages.

## Finding 2: the Indian retail friction ledger

For a delivery round trip at retail size, the all-in ledger includes STT on
both legs, buy-side stamp duty, exchange and SEBI charges with GST, a
depository charge per sell, slippage (assumed 10 bps per side), dividend
income taxed at the marginal slab rate (31.2% in this model), short-term
capital gains at 20% — and Section 94(7) of the Income-tax Act, which
disallows capital losses up to the dividend amount when the purchase falls
within three months before the record date and the sale within three months
after. Every dividend-capture trade sits squarely inside both windows, so the
clause is not a footnote: in the simulation it bit 82,704 of 172,220 trades.

Priced together, the wedge runs **roughly 0.85–0.9% per event**. A useful
self-check: simulating a "buy and sell the same close" control cell produces
−0.35%, matching the cost model's prediction to the basis point.

## Finding 3: the grid, and an honest split

I simulated every event across a grid of entry timings (0–20 sessions before
ex-date — the long entries proxy announcement timing, since declarations
precede ex-dates by weeks) crossed with exit timings (0–10 sessions after),
at fixed notional, every trade through the full friction model: 172,220
simulated trades. Events through 2022 tuned the grid; 2023 onward was touched
once, after selection.

Two results:

1. **Classic short-window dividend capture loses money everywhere** — every
   cell entering 1–5 sessions before ex-date is negative after frictions, in
   both periods. Given finding 1 and finding 2, it never had room.
2. **The long-entry region looked genuinely good**: the cell chosen on the
   tuning years (enter 20 sessions before ex, exit at the ex-date close)
   returned a median +1.02% per event in tune and **+0.95% on 2,158
   validation trades it never saw**, with all twelve tune-positive cells
   staying positive out-of-sample.

If I had stopped here, this would be a positive result. Stopping here is how
most positive results in this genre get published.

## Finding 4: the benchmark and the stress battery

Every validation trade was then paired with the index over the *identical*
dates — the same capital parked in NIFTY, benchmarked frictionless, so the
accounting leans against the strategy. The decision rule was fixed before
computing anything: the excess had to stay positive under 3× slippage, with
the bottom liquidity tercile excluded, with special dividends excluded, and
with the top five winners removed.

| stress | median excess vs NIFTY | beat rate |
|---|---|---|
| baseline | +0.20% | 51% |
| slippage 2× | +0.03% | 50% |
| **slippage 3×** | **−0.13%** | 49% |
| ex bottom-liquidity tercile | +0.19% | 51% |
| regular dividends only | +0.18% | 51% |
| top 5 winners removed | +0.18% | 51% |

The strategy's +0.95% stood against the index's **+0.73%** over the same
windows: most of the apparent edge was market drift wearing a dividend
costume. The residual +0.20% was robust to composition — it was not
concentration, illiquidity, or a few special situations — but it could not
pay for execution. Zero of the twelve surviving cells clear the 3× slippage
stress.

**Verdict: the edge dies.** A 20-basis-point advantage at a 51% beat rate is
an execution-cost measurement error away from zero, and the pre-committed
rule said so mechanically.

## Epilogue: the edge that tried to come back

One diagnostic survived the wreck: a handful of very-high-yield "special"
payouts had looked strikingly good — on seven events, which is to say, on
nothing. So I gave "special dividend" a real definition (a payout more than
three times the company's trailing median, or an outright yield above 5%,
both computed point-in-time) and joined the flag back onto the stored trade
logs.

The result looked like a resurrection: **309 out-of-sample trades at +1.14%
median excess over the index**, clearing even the harshest slippage stress.
This is the moment a backtest writes its own press release.

It was stopped by the dullest control in the study: a pre-committed sanity
check that flagged events must be a small minority. They weren't — 11% of
all dividends had been labeled "special", and inspecting the flags showed
why. **A trailing median lags a steadily growing payout for years**, so the
rule was flagging the ordinary annual dividends of companies that had simply
grown their payouts — one large cement company's regular finals were flagged
five years running. The cohort wasn't special situations; it was dividend
growers. The check was wired to void the conclusion mechanically, and the
verdict stayed dead.

I kept two things from the epilogue. First, a labeling rule is an instrument
and deserves the same validation as a price feed — plausible-on-paper
definitions fail against ground truth just like data does. Second, the
mislabeled cohort is a *different hypothesis* (something like a quality tilt
among dividend growers), and the honest response to accidentally surfacing
it is to write it down as a question — not to promote it to a finding on the
strength of numbers produced by a broken flag.

## What I take from a negative result

- The ex-date drop ratio on NSE large caps is ~0.87 and stable — a clean,
  reusable microstructure measurement regardless of the trading verdict.
- The anticipation drift into dividend events is real but is almost entirely
  the market; the paired-window benchmark is the single most clarifying step
  in the whole study, and it costs twenty lines of code.
- Pre-committing the split, the selection rule, and the kill criteria — in
  writing, in the code, before the numbers — is what let this end in a
  paragraph instead of a slow drift into curve-fitting. It then did so a
  second time, when a sanity check outranked 309 flattering trades. The
  strategy was rejected before it cost anything, which I count as the system
  working.

---

*This is personal research on public end-of-day data, shared for its
methodology. It is not investment advice, not a recommendation regarding any
security, and describes no forward-looking signals. Past behaviour of prices
around dividends does not predict future behaviour; Indian tax treatment
described here is simplified and specific to one profile. Consult a licensed
advisor before acting on anything dividend-shaped.*
