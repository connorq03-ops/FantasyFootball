# FantasyFootball — 10-Team, 2-QB Dynasty/Keeper Auction Board

Python tool that prices an auction draft board for a **10-team, 2-QB (superflex)
dynasty/keeper league**. It averages player values across sites, applies pool
scarcity adjustments, and re-prices the available pool so its dollar values sum
to the **actual remaining pot after keepers**.

Architecture mirrors the client + cache + prefetch pattern from
`connorq03-ops/NCAAProjectCH` (`golf/datagolf_client.py`, `golf/golf_app.py`,
`golf/validate_api_responses.py`, and the `SQLiteCache` in `app.py`). This repo
is self-contained — those patterns were copied, not imported.

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # only needed for local runs outside Devin
```

### Environment variables

| Variable | Purpose |
| --- | --- |
| `FANTASYPROS_API_KEY` | FantasyPros public API key, sent as the `x-api-key` **request header** (not Bearer, not `?key=`). Provided as an **org secret** at runtime in Devin sessions. |

The client reads `os.getenv('FANTASYPROS_API_KEY')` and falls back to a legacy
`FantasyPros` variable name. A premium key is required for full responses — a
free-tier key answers with `"tier":"free","limit":10` and truncates every
rankings response to the top 10 players. The key is never hardcoded or committed; `.env` is
gitignored and `.env.example` documents the variable name only.

---

## League API filters (and why they matter)

Base URL: `https://api.fantasypros.com/public/v2/json`

Every rankings/ADP call **must** carry the filters that describe this league, or
the tool silently pulls the wrong (1-QB, standard-scoring, redraft) market:

| Param | Value | Why |
| --- | --- | --- |
| `sport` | `NFL` | — |
| `scoring` | `PPR` | Full-point PPR (other valid values: `HALF`, `STD`). |
| `position` | `OP` | "Offensive Player" = superflex. Prices QBs at true 2-QB value. **Never use `ALL`** — that returns 1-QB rankings which badly undervalue QBs. |
| `type` | `dynasty` | Keeper/dynasty valuations, not redraft. |
| `season` | current | Plus `week` where the endpoint requires it. |

These live in exactly one place — `config.yaml` under `api_filters:` — and are
applied automatically by every `FantasyProsClient` method and by `prefetch.py`.
Each method accepts overrides for one-off pulls (e.g. per-position QB/RB/WR/TE
fetches), but the **overall board baseline always comes from the superflex
(`OP`) rankings**.

Endpoints used (confirmed against live responses):

- `GET nfl/{season}/consensus-rankings?type=dynasty&position=OP&scoring=PPR` — dynasty/ECR board baseline
- `GET nfl/{season}/consensus-rankings?type=adp&position=ALL&scoring=PPR` — ADP. FantasyPros only publishes ADP for `position=ALL` (the `OP` filter returns zero ADP rows), so `api_filters.adp_position` defaults to `ALL`. ADP is informational only — it never feeds the baseline — so no 1-QB pricing leaks into values.
- `GET nfl/players?position=ALL` — player id/metadata universe (ids, positions, teams)

### Modeling implication (do not double-count the QB premium)

Because superflex (`OP`) baselines **already embed the 2-QB QB premium**, the
`PremiumFactor` step in `value_model.py` handles only **scarcity within the
available pool**. It must **not** re-apply a blanket QB-position premium on top
of superflex baselines — that double-counts. `position_multipliers` in
`config.yaml` default to `1.0` for this reason. If a future data source offers
only 1-QB baselines, apply a QB uplift to **that source's column** before
averaging, never in `PremiumFactor`.

---

## Rate limit & caching strategy

The API budget is a **hard daily cap on a rolling 24-hour window** (`500/day`
on the premium key, `50/day` on a free key — set `rate_limit.max_calls`
accordingly), plus **1 request/second**, which the client enforces by sleeping.

Caching is therefore mandatory, not optional:

- `SQLiteCache.check_rate_limit(api='fantasypros', max_calls=..., window_seconds=86400)` guards every live call; only true cache misses count against the budget, and `cached_call` raises `RateLimitExceeded` with a clear message when the budget is gone.
- Default cache TTL is **24 hours**, so one daily prefetch serves every board build.
- Cache keys include the filter params (scoring/position/type/season), so different formats never collide.
- Raw pulls are snapshotted (timestamped JSON in `snapshots/`) for mid-draft re-runs.
- `prefetch_cli.py` costs ~3 calls (+1 per optional per-position pull).
- `build_board.py` makes **zero** API calls in the normal path — it fails loudly rather than spending budget unless you pass `--allow-api-calls`.
- `validate_api_responses.py` hits every endpoint **live** and consumes the budget: **run it sparingly** (endpoint changes / new season only).

---

## Usage

```bash
python prefetch_cli.py                    # once per day: fill the cache (~3-7 calls)
python build_board.py                     # build the board off cache (0 calls)
python build_board.py --sold sold.csv     # live draft mode
python build_board.py --mode replication  # old spreadsheet's constant multipliers
python validate_api_responses.py          # LIVE calls — run sparingly
python -m pytest tests                    # offline tests (no API calls)
```

Outputs land in `output/` (gitignored), timestamped:

- `board_<ts>.csv` — the full board
- `team_budgets_<ts>.csv` — per-team `Manager, KeeperSpend, AvailableBudget`
- `position_sanity_<ts>.csv` — optional 2-QB position sanity report

---

## Inputs

### `keepers.csv` (shipped, seeded with this year's 30 keepers)

`Team, Manager, Player, KeeperCost, KeeperYear`

All 30 rows are kept this year, so each keeper is flagged `IsAvailable = 0`.
`KeeperYear` `3/3` = final keeper year (cannot be kept again next season);
`2/3` = one more keeper year remains — stored for reference, no effect on this
year's pot math.

### `team_budgets.csv` (shipped)

`Team, Manager, StartingBudget, KeeperSpend, AvailableBudget`

`StartingBudget` is $200 for all 10 teams. The tool **recomputes**
`KeeperSpend` / `AvailableBudget` from `keepers.csv` rather than trusting the
seeded values.

### `espn_baselines.csv` (optional, gitignored)

`Player, ESPN_Baseline` — a second site's dollar values, joined via
`names.py` fuzzy matching. Add more sites by listing their columns in
`config.yaml → value_model.baseline_columns`.

### `sold.csv` (optional, live draft mode, gitignored)

`Player, SoldPrice, WinningTeam`

---

## Team / manager mapping

| Team | Manager | KeeperSpend | AvailableBudget |
| --- | --- | --- | --- |
| AL | Andrew Latzke | 20 | 180 |
| LATZ | Nick Latzke | 56 | 144 |
| RV | Blake Doerring | 120 | 80 |
| SDC | Isaac Kim | 73 | 127 |
| 40m | Connor Haley | 68 | 132 |
| KAHN | Aiden Kahn | 42 | 158 |
| B2B | Sebastian Canizares | 35 | 165 |
| iKim | John Gormley | 48 | 152 |
| DAL | Chas Sheffield | 51 | 149 |
| TINT | Nick DeFrancisco | 112 | 88 |

**There are two Nicks:** keeper rows labeled `Nick` belong to **Nick Latzke**
(LATZ) and rows labeled `Defran` belong to **Nick DeFrancisco** (TINT). The
seed CSVs use full manager names to disambiguate.

---

## 2-QB keeper value workflow

1. **Keepers:** each manager keeps 3 players; keeper cost is charged against
   that team's $200 budget.
2. **Remaining pot:** `remaining_pot = sum(StartingBudget) - sum(KeeperCost) = 2000 - 625 = 1375`.
3. **Team caps:** each team's `AvailableBudget = StartingBudget - team_keeper_cost`
   is that team's **max-spend cap**, *not* a re-pricing input. The board is a
   single shared market priced to the league-wide `remaining_pot` of $1,375.
4. **Board:** the available pool is re-priced so its `FinalAdj` dollars sum
   exactly to `remaining_pot`.

---

## Value model

Columns per player:

`Player, Position, Team, Bye, FP_Baseline, ESPN_Baseline, Tag, IsAvailable,
Avg_Baseline, RankAvail, PremiumFactor, LowValueFactor, RawAdj, MarketScalar,
FinalAdj, PosRankByAdj, Key, Tier`

| Column | Definition |
| --- | --- |
| `FP_Baseline` | FantasyPros value from the **superflex/PPR/dynasty** pull. FantasyPros publishes ranks, so `rank_to_baseline()` converts ECR rank to a value via `top_value * exp(-(rank-1)/decay)` (knobs in config). |
| `ESPN_Baseline` | Optional second-site value from `espn_baselines.csv`. |
| `Avg_Baseline` | Mean of the per-site baseline columns, ignoring missing sites. Example: FP 34, ESPN 46 → 40.0. Add sites in config and they're averaged automatically. |
| `IsAvailable` | 1 = draftable; 0 = keeper (or sold, in live draft mode). |
| `RankAvail` | Rank among `IsAvailable == 1` players by `Avg_Baseline` descending. |
| `PremiumFactor` | Smooth, tunable **scarcity** curve of `RankAvail`: `1 + (peak-1) * exp(-(rank-1)/decay)`, with a deep-tail floor. Defaults seeded from the spreadsheet (top overall ~1.4, next tier ~1.2–1.25, most 1.0, tail 0.9). **No blanket QB premium here.** |
| `LowValueFactor` | Configurable haircut (default 0.8) beyond a configurable rank/baseline cutoff, else 1.0. |
| `RawAdj` | `Avg_Baseline * PremiumFactor * LowValueFactor`. |
| `MarketScalar` | Single solved global multiplier (see below). |
| `FinalAdj` | `round(RawAdj * MarketScalar)`, $1 floor, reconciled so the available pool sums exactly to `remaining_pot`. |
| `PosRankByAdj` | Rank within position by `FinalAdj` descending. |
| `Key` | `f"{Position}|{PosRankByAdj}"`. |
| `Tier` | Bucket from configurable `FinalAdj` breakpoints (keepers are tagged `Keeper`). |

### Pot-solving (default) vs. replication mode

- **`pot_solve` (default):** `MarketScalar = remaining_pot / sum(RawAdj over available players)`. This collapses the old spreadsheet's separate constants `InflationFactor` (1.3) and `Scale` (0.9), which were mathematically redundant global multipliers, into one solved scalar. After rounding, a $1 floor is applied and the leftover rounding remainder is distributed to the top players so `sum(FinalAdj) == remaining_pot` **exactly**.
- **`replication` (`--mode replication`):** faithful replication of the old sheet using the constant `1.3 * 0.9` multipliers, with no pot reconciliation.

### Optional 2-QB position sanity check

`position_sanity_check()` compares summed `FinalAdj` by position against
expected roster spend (10 teams × starting slots, including **2 QB**). If QB
dollars look understated, the pull probably wasn't superflex (`OP`).

---

## Live draft mode

```bash
python build_board.py --sold sold.csv
```

Players in `sold.csv` are marked unavailable and tagged with their price; the
sale price is subtracted from **both** the league-wide remaining pot and the
winning team's available budget, then the remaining board is re-solved against
the true remaining money — all off cached data, with **no new API calls**.

---

## Files

| File | Purpose |
| --- | --- |
| `fantasypros_client.py` | `FantasyProsClient` — header auth, league-filter defaults, 1 req/sec throttle. |
| `cache.py` | `SQLiteCache` (TTL + rolling-window rate limiter), `cached_call`, raw snapshots. |
| `prefetch.py` | `prefetch_all_player_data()` — bulk daily pull, indexed by player id and normalized name. |
| `prefetch_cli.py` | Daily prefetch entry point. |
| `names.py` | Name normalization + fuzzy matching (suffixes, D/ST, `JSN`, `Amon Ra`, manual overrides). |
| `value_model.py` | Composable pipeline functions + `run_value_model()`. |
| `build_board.py` | End-to-end board build, budget summary, live draft mode. |
| `validate_api_responses.py` | Live endpoint/shape validation (**consumes API budget**). |
| `config.yaml` / `config.py` | League filters, roster settings, model knobs, rate limit, TTL, mode flags. |
