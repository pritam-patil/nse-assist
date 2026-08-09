# The dividend study — what was asked, what was found, why it's closed

*Personal note, written 2026-08, updated after the specials retrofit. The
numbers quoted here are frozen from RESULTS.md; the scripts regenerate them,
this note interprets them. If you are rereading this because a new dividend
idea feels exciting: that is exactly the state of mind this note was written
for — and the epilogue below is the freshest example of why.*

## The question

Indian dividends are taxed at slab (31.2% for me); prices allegedly drop by
less than the dividend on ex-date. Is the gap — or the anticipation drift into
the event — tradable at retail size after every real friction, including
Section 94(7)? Five bursts plus one retrofit, each with its criteria
pre-committed before the numbers existed.

## The data, and the bug the validation caught

Ten years of daily bars plus dividend events for the NIFTY 500, per-symbol
parquet under `data/cache/`, warmed by `data/fetch.py`. Three payouts were
pinned in params.yaml from public announcements (COALINDIA, BLUEDART,
BAYERCROP, Aug 2025) before trusting the feed; `data/events.py` refuses to
bless a table that contradicts them.

That validation paid for itself on day one: the first events build assumed
Yahoo's closes were raw and split-adjusted them — but Yahoo split-adjusts
everything at source (closes, volumes, dividend amounts; verified against
TATASTEEL's 2022 1:10 split). The double adjustment surfaced as a 51% "yield"
on a ₹9.96 close. The regression test pins the shape. Lesson kept: **never
infer a feed's adjustment basis from documentation or habit — verify it
against a corporate action you can check by hand.**

## Finding 1 — the drop ratio is 0.87

Close-to-close, NIFTY-adjusted, 4,973 events: the price gives up a median
**87% of the dividend** where yield ≥ 1% (n=1,420), stable at 0.85–0.97 across
every bucket where 1/yield noise permits measurement. So ~13% of the dividend
is "left on the table" pre-tax — which sounds like an edge until you price
what collecting it costs.

## Finding 2 — the friction ledger decides everything

`data/frictions.py`, fully parameterized from params.yaml: STT both legs,
stamp duty, exchange/SEBI/GST, DP charge, 10 bps slippage per side, dividend
at slab, STCG 20%, and 94(7) disallowing capital losses up to the dividend
inside its two three-month windows. Hand-computed tests pin it; the canonical
94(7) trade costs exactly stcg × disallowed = ₹240 versus the same trade
without the clause. The all-in wedge runs **~0.85–0.9% per round trip** on the
grid — the (0,0) control cell (buy and sell the same close) measures −0.35%,
matching the prediction to the basis point. The friction model and the
simulator agree about reality; that cross-check is the spine of the whole
study.

## Finding 3 — the grid, split, and what survived

172,220 trades: 7 entries (20/15/10/5/3/1/0 sessions before ex) × 5 exits
(0/1/3/5/10 after), ₹100k notional, tuned on ≤2022, validated on 2023+.

- **Classic short-window capture is dead twice over**: entry 1–5 sessions
  before ex is negative after frictions in the tuning years AND the
  validation years. The 0.87 drop ratio plus slab tax plus ~0.9% frictions
  never had room to clear zero.
- **The anticipation region looked real**: e=20/x=0 selected on tune (+1.02%
  median) held +0.95% at a 56% win rate across 2,158 unseen trades, and all
  12 tune-positive cells stayed positive. This is where burst 4 stopped, with
  the drift caveat attached in writing.

## The verdict — and why it was designed to be mechanical

Burst 5 paired every validation trade with NIFTY over the identical dates and
ran the battery under a rule fixed before any number was computed. The two
numbers that decide it: **strategy +0.95%, NIFTY +0.73%**. The true excess was
+0.20% per event at a 51% beat rate — it survived excluding illiquid names,
excluding specials, and removing the top five winners (so it was not
concentration), but slippage at 2× left +0.03% and **3× killed it at −0.13%**.
Zero of twelve cells clear 3×. Burst 4's caveat was correct: most of the edge
was the market.

**Edge dies.** A 20 bps advantage cannot pay 20 bps of extra execution. The
strategy was rejected before it cost anything, which is the system working.

## Epilogue — the edge that tried to come back, and the check that stopped it

Burst 5 left specials as a diagnostic: +4.20% excess on **seven** yield-flagged
events. The retrofit (`data/study_specials.py`) gave "special" a real
definition — amount > 3× the symbol's trailing median payout, or yield > 5%,
point-in-time — and joined the flag onto the stored trade logs without
re-simulating anything.

The result looked like a resurrection: **309 out-of-sample trades in the
selected cell at +1.14% median excess**, clearing even the 3× slippage bound.
The pre-committed change-rule would have qualified the verdict — except the
*other* pre-committed check fired first: 11.2% of all events flagged is not a
small minority. Eyeballing the amount-rule flags (482 of 560) exposed the
defect: **a trailing median lags a steadily growing payout for years**, so
UltraCemco's ordinary 37/38/38/70/77.5 finals were flagged five years running,
Tata Elxsi's for four, ICICI Bank's ₹8 annual dividend labeled "special". The
+1.14% cohort is substantially *dividend growers*, not special situations, and
the sanity gate voided the verdict change mechanically — the section in
RESULTS.md records the failure, the exemplar, and "verdict unchanged".

Three lessons filed:

1. **Sanity checks must gate conclusions, not decorate them.** The check was
   in the spec ("flagged should be a small minority"); wiring it to void the
   verdict is what made 309 pretty trades unable to sneak past it.
2. **A definition is an instrument and needs its own validation** — the flag
   rule got the same treatment the price feed got in burst 0, and failed the
   same way: plausible on paper, wrong against known ground truth.
3. **The mislabeled cohort is a different hypothesis, accidentally
   surfaced**: +1.14% OOS excess on dividend-*growers* at the 20-session
   entry smells like a quality/momentum tilt. If that itch ever needs
   scratching, it is a NEW study with a NEW pre-committed gate — not a rerun
   of this one.

## What would legitimately reopen this (and what wouldn't)

Would:
- **Measured, not assumed, execution costs.** The verdict hinges on the
  slippage stress. If live paper fills on liquid names demonstrated ≤10 bps
  reliably, the baseline +0.20% excess stands un-stressed — thin, but the
  question changes from "does it survive 3×?" to "what is real slippage?"
- **A specials study with a valid instrument.** The yield-only diagnostic
  (n=7, +4.20%) still stands as an anecdote. A real attempt needs a flag that
  survives its own sanity check — candidates: 3× the trailing **max**, a
  short trailing window that tracks growth, or amount-jump AND elevated
  yield — pre-committed before measurement, ideally sourced from
  announcement data that labels specials explicitly.

Wouldn't:
- Re-gridding with more cells, other windows, or other notional. The split
  already spent the tuning data.
- Removing the slab-tax assumption. That is my actual tax rate.
- "The market has changed." Come back with new out-of-sample years, not a
  new story about old ones.
- Trusting the +1.14% dividend-grower number as it stands. It was measured
  on a cohort assembled by a broken instrument, and it keeps its scare
  quotes until an honest definition reproduces it.

## Where everything lives

RESULTS.md (marker-owned sections, one per burst) · `data/fetch.py` (cache) ·
`data/events.py` (event table + pinned validation + the special flag) ·
`data/study_exdate.py` (drop ratio) · `data/frictions.py` (cost model) ·
`data/backtest.py` (grid, fingerprint-guarded resume) · `data/study_grid.py`
(split + heatmaps) · `data/study_stress.py` (battery + verdict) ·
`data/study_specials.py` (the retrofit and its sanity gate). 563 tests.
Nothing in `src/` depends on any of it.
