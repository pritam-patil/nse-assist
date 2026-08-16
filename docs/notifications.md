# Research-side notifications

Dividend digest and signal alerts over Telegram, for the research side (the
`data/` dividend study — separate from the `src/` trading pipeline). Two message
types, defined in `data/notify.py`:

- **Digest** — one per day, market-wide: new dividend declarations, the
  model-eligible ones ranked by expected net return with the dispersion caveat,
  plus a fenced "FYI, outside model scope" section for notable ineligibles.
- **Signal alerts** — urgent eligible events, each paper-logged at send time so
  an alert and its paper record can never diverge. Capped at 5/day, ranked by
  expected net.

Today both are effectively empty of signals: **no parameter cell survives the
burst-7 bar**, so the model scope is empty and the digest says so in those
words. The plumbing is live so that if measured execution costs ever change the
verdict, the alerts start on their own — but nothing is being recommended now.

## The bot and secrets

A **separate personal bot** from the pipeline's, so the research side never
shares Telegram `getUpdates` with it (this module only ever *sends*, so even
sharing one bot cannot conflict — but a separate bot keeps the two streams
legible). To create it:

1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts.
2. Put the token in `.env` as `NOTIFY_TELEGRAM_BOT_TOKEN` (never committed).
3. Message your new bot once, then read your numeric chat id from
   `https://api.telegram.org/bot<token>/getUpdates` and set
   `NOTIFY_TELEGRAM_CHAT_ID`.

Blank `NOTIFY_*` falls back to the pipeline bot's `TELEGRAM_*`. For the GitHub
workflow, add the same values as **repository secrets** (Settings → Secrets and
variables → Actions), under the `prod` environment: `NOTIFY_TELEGRAM_BOT_TOKEN`
and `NOTIFY_TELEGRAM_CHAT_ID`, or rely on the existing `TELEGRAM_*` fallback.

## The workflow

`.github/workflows/notify.yml` runs twice daily:

| Cron (UTC) | IST | Purpose |
|---|---|---|
| `0 13 * * *` | 18:30 | Post-market digest |
| `30 17 * * *` | 23:00 | Late-filings catch-up (**the parameter**) |

The second time is the one to change. GitHub Actions requires a literal cron, so
there is no variable to set — edit that one line. The documented **pre-market
alternative** is `30 1 * * *` (01:30 UTC = 07:00 IST), for a morning read before
the open.

**Step 1 is an access assertion.** A GitHub runner is an untested network — cloud
egress reaches NSE differently from a residential connection (the Workers spike
got empty bodies where a laptop got data). So the job first pings the
announcements endpoint (`data.upcoming --assert-access`); on failure it sends a
Telegram warning and exits nonzero. It never fails silently, because a notifier
that goes quiet is indistinguishable from a quiet market.

**State is committed back to the repo.** `data/notify_state.json` (the seq_id
seen-ledger, the last-digest date, the daily alert counter) and `data/paper/`
(the paper ledger) are committed by the workflow, because a runner is a fresh
checkout each run — without persistence the 23:00 run would forget the 18:30 one
and the daily cap would reset. The seen-ledger keyed by seq_id is what makes the
second run surface only unseen filings.

## Liquidity snapshot — the runner's stand-in for the price cache

`data/cache/` (ten years of price history, ~47MB) is gitignored — too large to
commit, and `data.fetch` regenerates it in minutes. `data/grid/` (the backtest
trade logs) IS committed, so a runner has the trades. But computing whether
any of them beat NIFTY needs the index's own closes too — `data/grid/` alone
was not enough; `notify._survivors()` still fell back to its empty answer on
a bare runner until `data/nifty_snapshot.py` (below) closed that second gap.
Separately, the digest's yield estimates and liquidity flags need a
**current close** and **recent turnover** per symbol — neither the grid nor
the NIFTY snapshot carries either.

`data/liquidity_snapshot.csv` closes that last gap: a few KB, one row per
symbol, just the latest close and 60-session average turnover. Committed, so
a runner without the full cache still gets real (if slightly stale) numbers
instead of "unknown" on every row. Refresh it locally, periodically — weekly
is plenty, since a yield estimate only breaks if the price moved enough to
cross a bucket boundary in the meantime:

```bash
python -m data.liquidity_snapshot   # needs the local price cache — run data.fetch first if stale
git add data/liquidity_snapshot.csv
git commit -m "data: refresh liquidity snapshot"
```

`cache_context()` in `upcoming.py` always prefers the live cache when present
and only falls back to the snapshot for symbols it can't answer — on a bare
runner, that's every symbol. The digest logs how many rows it recovered this
way and the snapshot's as-of date, so a stale number is visible, not silent.

## NIFTY snapshot — what `data/grid/` alone didn't cover

`data/grid/`'s trades need an index close to be compared against — that's
how "beats NIFTY" is computed at all — and a bare runner has none of the
index's own price history locally either. `data/nifty_snapshot.csv` is the
same idea as the liquidity snapshot, scaled down further: just `date, close`
for NIFTY, ~64KB, refreshed and recommitted the same way:

```bash
python -m data.nifty_snapshot
git add data/nifty_snapshot.csv
git commit -m "data: refresh NIFTY snapshot"
```

**The coupling that actually matters here** spans THREE files, not two:
`data/grid/`, `data/nifty_snapshot.csv`, and `data/events.parquet` (below) —
all three must move together whenever the backtest is rerun. `with_nifty()`
degrades quietly if the NIFTY snapshot falls behind (it drops unpaired
trades rather than erroring), so a stale one costs silent under-coverage,
not a crash.

`study_exdate.nifty_closes()` tries the live cache first (so a local run is
always current), falls back to this snapshot when the live cache is empty,
and only raises when neither source has anything at all.

## `data/events.parquet` — the third file, found from real logs, not review

`data/grid/`'s raw trade rows (symbol, dates, prices) are not enough on their
own: `study_grid.with_context()` — the step that joins in `yield_pct` and
`avg_volume_60d`/`avg_price_60d` to derive liquidity — reads
`data/events.parquet` directly. Committing `data/grid/` and
`data/nifty_snapshot.csv` closed two gaps but left this one; on a bare
runner `_survivors()` kept failing with a plain `FileNotFoundError` and
degrading to its fail-safe empty answer, now for a *third* distinct reason.

This one was caught from the actual GitHub Actions run logs, not from local
testing — the local "bare runner" simulation used to verify the NIFTY
snapshot only blanked out the price cache (`fetch.CACHE_DIR`), not
`events.EVENTS_PATH`, so it kept silently reading the real local
`data/events.parquet` and never exercised this path. The lesson kept: when
simulating "what a bare checkout sees," isolate every path a committed-vs-
gitignored artifact could resolve to, not just the one you're actively
fixing — a real CI run is still the only fully faithful test.

`data/events.parquet` (259KB, ~5,000 dividend events) is now committed
alongside `data/grid/`. Its own fingerprint mismatch against the grid's
`meta.json` is expected and harmless (it predates the specials-flag column;
`study_specials.join_flags()` is the real integrity check, not the raw
fingerprint — see `data/study_specials.py`). Refresh it with
`python -m data.events` whenever the backtest is rerun, in the same commit
as the other two.

## `make notify` — the manual / local path

```bash
make notify PYTHON=venv/bin/python
```

Runs the same sequence as the workflow: assert access, refresh the calendar,
alerts before digest. `make notify-dry` previews the messages without sending;
`make notify-check` runs only the access assertion. `make help` lists targets.

## Laptop-cron — the backup path if runners are blocked

If GitHub runners turn out to be blocked from NSE (the access assertion keeps
failing — a real possibility given the Workers spike), run the notifier from a
machine on a residential connection instead. Add to your crontab (`crontab -e`),
adjusting the path and using **UTC** times to match the workflow — or your local
equivalent:

```cron
# nse-assist dividend notifier — laptop backup for the GitHub workflow.
# Load .env, cd to the repo, run make notify. Times are the machine's local
# clock; 18:30 and 23:00 IST shown here for an IST laptop.
CRON_TZ=Asia/Kolkata
30 18 * * *  cd /path/to/nse-assist && set -a && . ./.env && set +a && make notify PYTHON=venv/bin/python >> /tmp/nse-notify.log 2>&1
0  23 * * *  cd /path/to/nse-assist && set -a && . ./.env && set +a && make notify PYTHON=venv/bin/python >> /tmp/nse-notify.log 2>&1
```

Notes:

- **Do not run both paths at once** for the same window — they both commit
  `data/notify_state.json`, and a laptop that has not pulled will conflict with
  the runner's commit. Pick one path per window; if you switch to laptop-cron,
  disable the workflow (comment out its `schedule`).
- `CRON_TZ` needs a cron that supports it (Linux/vixie-cron, and macOS via
  launchd is cleaner — a `launchd` plist is the mac-native equivalent).
- The laptop path has the live price cache, so its yield/liquidity numbers are
  always current; a runner falls back to the committed snapshot above, which
  is only as fresh as the last local refresh. Survivors are computed from the
  committed grid either way — both paths agree on model scope.

## What this does NOT do

No order placement exists anywhere in this codebase. The notifier composes text
to one private chat; its only side effects are the committed state file and
appends to the paper ledger. The real-money gate (`data/paper.py`) is not
cleared, and the standing verdict is that the strategy does not beat the index
after realistic costs.
