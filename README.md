# nse-assist

A single-user daily assistant for the NIFTY 100. Each evening it pulls the day's
bars, computes indicators, runs a small set of rules over the universe, fills and
exits **paper** trades against committed risk limits, refreshes mutual-fund NAVs,
and sends the whole picture to Telegram.

It does not place orders. There is no broker integration and none is planned —
the ledger in `paper_trades` is the product.

Runs entirely on free tiers: GitHub Actions, Yahoo's public chart endpoint, the
AMFI NAV dump, and the Telegram Bot API. No API keys beyond the Telegram bot.

## Stages

```
ingest    NSE bhavcopy -> yfinance fallback         -> prices
features  SMA / RSI / ATR / relative volume        -> (computed, never stored)
signals   rules -> dated, sized entries            -> signals
journal   fills, exits, daily limits               -> paper_trades
funds     AMFI NAV dump (needs FUND_SCHEME_CODES)  -> fund_navs
deliver   the day's report                         -> Telegram
```

Plus two that are not part of the daily run: `backtest` (replays the rules over
stored history — minutes, not seconds) and `doctor` (health check).

```bash
python main.py --stage all
```

```bash
python main.py --stage doctor
```

`--dry-run` skips every write and every send. `--stage scan` is an alias for
`signals`. `--stage ingest --backfill` re-requests the full lookback for every
symbol rather than only the days since its newest stored bar; you need it after
raising `INGEST_LOOKBACK_DAYS`, because the incremental window only grows forward.

## Setup

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`. Everything else there is an optional override, with one
exception worth setting: `FUND_SCHEME_CODES`. The funds stage skips entirely
while it is empty, because storing all ~14k AMFI schemes daily would add about a
megabyte a day to a database that gets committed after every run. Find a scheme's
code by searching the dump:

```bash
curl -s https://www.amfiindia.com/spages/NAVAll.txt | grep -i "parag parikh flexi"
```

Then run `--stage doctor`: it checks the environment, the live price feed, the
AMFI dump, the committed constants, and prints a row count per table.

The first `--stage ingest` pulls ~1500 days for 100 symbols. It takes about a
minute and writes roughly 100k rows.

## What is committed on purpose

Two modules hold decisions rather than configuration, so a change to either shows
up as a reviewable diff instead of an invisible environment difference:

- **`src/universe.py`** — the NIFTY 100 constituents. Fetching them would mean an
  index reconstitution silently changing what last night's scan looked at.
- **`src/holidays_2026.py`** — NSE's published trading calendar. Ingest uses it to
  decide whether a date had a session at all, and that answer must not change
  because an API was briefly unreachable. It covers 2026 only and *raises* outside
  that year rather than defaulting to "open".
- **`src/risk_config.py`** — `capital_per_trade`, `max_daily_loss`,
  `daily_profit_target` and the sizing parameters derived from them. A backtest can
  then be re-run against the exact limits that were live at the time.

`output/nse.db` is also committed (see `.gitignore`), so each scheduled run
inherits price history, open signals and the ledger from the run before it. It is
about 9 MB with the full universe backfilled, and grows slowly — a few hundred
rows a day — as long as `FUND_SCHEME_CODES` stays narrow.

## Things worth knowing before trusting a number

- **Sizing is volatility-based.** `risk_config.max_shares()` takes the smaller of a
  risk-derived and a capital-derived count, so a wider stop buys fewer shares rather
  than risking more money.
- **Small capital silently shrinks the universe, so the shrinkage is reported.** One
  share is the smallest tradable unit, and a stock priced near or above
  `capital_per_trade` cannot be sized at all. At ₹5,000 per trade that removes 24 of
  the 99 priced NIFTY 100 names — BOSCHLTD, SHREECEM, MARUTI and the rest of the
  expensive end — and pins another 20 at exactly one share, where the realised risk
  is set by the share price rather than by `risk_per_trade_fraction`. At the
  committed ₹25,000 only BOSCHLTD and SHREECEM are excluded, which is unavoidable —
  one share of either costs more than the whole position. Because the exclusion is
  price-biased and would otherwise be invisible, coverage appears in `--stage doctor`
  and every dropped firing is named by `--stage signals`. The doctor stage only
  *fails* when more than half the universe is unsizeable.
- **The three risk numbers are linked.** `risk_per_trade_fraction` is derived as
  `max_daily_loss / max_open_positions / capital_per_trade`, so a fully stopped-out
  book lands on the daily loss limit. `risk_config.assert_coherent()` fails the
  doctor stage if an edit breaks that relationship.
- **A daily limit beyond `max_open_positions` reach is a disabled limit.** Both
  limits are measured against P&L realised that day, and only an open position can
  realise anything — so at most `max_open_positions` trades can close in a day. A
  `daily_profit_target` above `max_open_positions × risk × reward_risk_ratio`, or a
  `max_daily_loss` above `max_open_positions × risk`, reads as armed in the config
  and can never trip. `assert_coherent()` fails the doctor stage on both, because
  this is the failure mode that looks the safest on paper.
- **Backtest exits are pessimistic on purpose.** When one daily bar contains both
  the stop and the target, the stop is taken. Daily data cannot say which came
  first, and assuming the good one is how a backtest ends up flattering a rule.
- **Fills use the next bar's open**, not the signal bar's close. A signal is
  generated after the close; filling at that close would be buying a price that had
  already passed.
- **Rule thresholds were measured, not chosen.** The relative-volume floor sits at
  1.0 and the pullback RSI band at 40/60 because the textbook values (1.2, and
  30/70) suppressed nearly every signal over ~800 sessions of testing. Re-measure
  with `--stage backtest` before moving them.
- **Two price sources, never blended within a date.** NSE's UDiFF bhavcopy is
  primary — the exchange's own record, one zipped CSV per session covering every
  symbol. yfinance is the fallback for a blocked IP, a format change, or a backfill
  too large to fetch a day at a time. `store_bars()` enforces precedence in the
  SQL conflict clause: bhavcopy overwrites a day the fallback filled, and the
  fallback never overwrites bhavcopy. Because a filled gap would otherwise move the
  watermark past itself and never be revisited, recent fallback dates are
  deliberately re-offered to bhavcopy each run.
- **Yahoo invents bars on NSE holidays.** Zero volume, open=high=low=close, previous
  close carried forward. There were 518 of them in the first backfill. Left in, they
  flatten RSI and drag the 20-day average volume down, so ingest rejects them on
  arrival and purges any that are already stored. `--stage doctor` fails if either
  invariant breaks.
- **yfinance adjusts prices by default.** `auto_adjust` defaults to `True`, which
  returns split- and dividend-adjusted values that silently disagree with
  bhavcopy's raw traded prices. It is explicitly set to `False`, and its float32
  widening (1307.800048828125 for a printed 1307.80) is rounded away.
- **Corporate actions break symbols, not the feed.** Three NIFTY 100 tickers were
  already stale when this was written — Tata Motors demerged into TMPV and TMCV,
  United Spirits trades as UNITDSPR, and LTIM no longer resolves at all. When
  ingest starts reporting a 404 for one symbol, look there first.

Paper trades. Not investment advice.
