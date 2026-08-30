"""
value_model.py - Auction value pipeline for a 10-team, 2-QB dynasty/keeper league.

Columns produced per player (mirroring the league spreadsheet):
    Player, Position, Team, Bye, FP_Baseline, ESPN_Baseline, Tag, IsAvailable,
    Avg_Baseline, RankAvail, PremiumFactor, LowValueFactor, RawAdj,
    MarketScalar, FinalAdj, MarketPrice, Edge, PosRankByAdj, Key, Tier

Pipeline (each step is a composable function operating on a pandas DataFrame):
    compute_avg_baseline -> compute_rank_avail -> apply_premium_factors ->
    compute_raw_adj -> solve_for_pot -> reconcile_to_pot -> market_price ->
    assign_pos_rank_and_key -> assign_tier

IMPORTANT — THE SOURCE BASELINE IS NEVER RESCALED
FP_Baseline (and any other *_Baseline column) is a published absolute auction
dollar value for the reference format in `baseline_auction`. Nothing in this
module writes to those columns: keeper availability, scarcity, the draft pool
and the remaining-pot solve all land in RawAdj / MarketScalar / FinalAdj, so
the source value stays auditable next to the league-adjusted price.

IMPORTANT — 2-QB / SUPERFLEX MODELING NOTE
The FantasyPros baselines are pulled with `position=OP` (superflex), so they
ALREADY price QBs at their true 2-QB value. `PremiumFactor` therefore models
only SCARCITY within the available pool; it must NOT re-apply a blanket
QB-position premium on top of superflex baselines (that would double-count the
2-QB premium). If a future data source offers only 1-QB baselines, apply a QB
uplift to THAT SOURCE's column before averaging — never here.
"""

import math
from typing import Any, Dict, List, Optional

import pandas as pd

REQUIRED_COLUMNS = [
    'Player', 'Position', 'Team', 'Bye', 'FP_Baseline', 'ESPN_Baseline', 'Tag',
    'IsAvailable', 'Avg_Baseline', 'RankAvail', 'InDraftPool', 'PremiumFactor',
    'LowValueFactor', 'RawAdj', 'MarketScalar', 'FinalAdj', 'MarketPrice', 'Edge',
    'PosRankByAdj', 'Key', 'Tier',
]


# ── Baselines ────────────────────────────────────────────────────────────────

def compute_avg_baseline(df: pd.DataFrame, config: Dict[str, Any]) -> pd.DataFrame:
    """
    Avg_Baseline = mean of the per-site baseline columns, ignoring missing sites.

    Add more sites to `value_model.baseline_columns` in config.yaml and they are
    averaged automatically. Example: FP 34, ESPN 46 -> 40.0. The source columns
    themselves are only parsed to numbers here, never rescaled.
    """
    df = df.copy()
    cols = config.get('value_model', {}).get('baseline_columns', ['FP_Baseline', 'ESPN_Baseline'])
    for col in cols:
        if col not in df.columns:
            df[col] = math.nan
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['Avg_Baseline'] = df[cols].mean(axis=1, skipna=True)
    return df


def compute_rank_avail(df: pd.DataFrame) -> pd.DataFrame:
    """RankAvail = rank among IsAvailable == 1 players by Avg_Baseline descending."""
    df = df.copy()
    df['IsAvailable'] = pd.to_numeric(df.get('IsAvailable', 1), errors='coerce').fillna(0).astype(int)
    df['RankAvail'] = pd.NA
    avail = df['IsAvailable'] == 1
    ranks = df.loc[avail, 'Avg_Baseline'].rank(ascending=False, method='first')
    df.loc[avail, 'RankAvail'] = ranks.astype('Int64')
    return df


def flag_draft_pool(df: pd.DataFrame, config: Dict[str, Any]) -> pd.DataFrame:
    """
    InDraftPool = 1 for the players the league can actually roster.

    Only `teams * roster_size` players leave the board all year, so pricing all
    ~400 available players spreads the pot over hundreds of names nobody bids
    on — the $1 floor alone hands a quarter of the money to undraftable depth
    and starves the real draft slots. Players outside the pool are carried at
    $0 (waiver fodder) and excluded from the pot solve.
    """
    df = df.copy()
    league = config.get('league', {})
    cfg = league.get('draft_pool', {})
    avail = df['IsAvailable'] == 1

    if not cfg.get('enabled', True):
        df['InDraftPool'] = avail.astype(int)
        return df

    roster_size = int(cfg.get('roster_size', 15))
    rosterable = int(league.get('teams', 10)) * roster_size
    slots = max(0, rosterable - int((~avail).sum()))
    ranks = pd.to_numeric(df['RankAvail'], errors='coerce')
    df['InDraftPool'] = (avail & ranks.le(slots)).astype(int)
    return df


# ── Premium / haircut ────────────────────────────────────────────────────────

def scarcity_premium(rank: Optional[float], position: str, config: Dict[str, Any]) -> float:
    """
    Smooth, tunable scarcity premium as a function of RankAvail.

        premium = tail_factor + (peak - tail_factor)
                   * exp(-(rank - 1) / decay)

    Defaults are fitted to the concentration profile of last season's prices.

    `position_multipliers` default to 1.0 and exist only for genuine pool
    scarcity tweaks — do NOT use them to add a QB premium on superflex data.
    """
    cfg = config.get('value_model', {}).get('premium', {})
    if rank is None or pd.isna(rank):
        return 1.0
    rank = float(rank)
    peak = cfg.get('peak', 1.05)
    decay = cfg.get('decay', 9.0)
    tail_factor = cfg.get('tail_factor', 0.95)
    premium = tail_factor + (peak - tail_factor) * math.exp(-(rank - 1.0) / decay)
    premium *= cfg.get('position_multipliers', {}).get(position, 1.0)
    return round(premium, cfg.get('round_digits', 3))


def apply_premium_factors(df: pd.DataFrame, config: Dict[str, Any]) -> pd.DataFrame:
    """Add PremiumFactor (scarcity curve) and LowValueFactor (configurable haircut)."""
    df = df.copy()
    df['PremiumFactor'] = [
        scarcity_premium(rank, pos, config)
        for rank, pos in zip(df['RankAvail'], df['Position'].fillna(''))
    ]

    lv = config.get('value_model', {}).get('low_value', {})
    factor = lv.get('factor', 0.80)
    rank_cutoff = lv.get('rank_cutoff')
    baseline_cutoff = lv.get('baseline_cutoff')

    def low_value(rank, baseline):
        if rank_cutoff is not None and not pd.isna(rank) and float(rank) > float(rank_cutoff):
            return factor
        if baseline_cutoff is not None and not pd.isna(baseline) and float(baseline) < float(baseline_cutoff):
            return factor
        return 1.0

    df['LowValueFactor'] = [
        low_value(rank, baseline)
        for rank, baseline in zip(df['RankAvail'], df['Avg_Baseline'])
    ]
    return df


def compute_raw_adj(df: pd.DataFrame) -> pd.DataFrame:
    """RawAdj = Avg_Baseline * PremiumFactor * LowValueFactor."""
    df = df.copy()
    df['RawAdj'] = (pd.to_numeric(df['Avg_Baseline'], errors='coerce').fillna(0.0)
                    * df['PremiumFactor'] * df['LowValueFactor'])
    df.loc[df['IsAvailable'] != 1, 'RawAdj'] = 0.0
    if 'InDraftPool' in df.columns:
        df.loc[df['InDraftPool'] != 1, 'RawAdj'] = 0.0
    return df


# ── Pot solving ──────────────────────────────────────────────────────────────

def solve_for_pot(raw_adj_values, remaining_pot: float, min_bid: float = 1) -> float:
    """
    MarketScalar that scales surplus over the $1 floor to the pot, not gross value.

    This single solved scalar replaces the spreadsheet's separate constant
    InflationFactor (1.3) and Scale (0.9), which were mathematically redundant
    global multipliers. The floor itself is funded before the surplus is
    distributed, rather than being silently clawed back from the elite tier.
    """
    values = pd.Series(list(raw_adj_values)).fillna(0.0)
    surplus = (values - float(min_bid)).clip(lower=0.0)
    total_surplus = float(surplus.sum())
    count = len(values)
    if total_surplus <= 0 or float(remaining_pot) < count * float(min_bid):
        return 0.0
    return (float(remaining_pot) - count * float(min_bid)) / total_surplus


def replication_scalar(config: Dict[str, Any]) -> float:
    """Faithful-replication mode: the old constant InflationFactor * Scale."""
    rep = config.get('value_model', {}).get('replication', {})
    return float(rep.get('inflation_factor', 1.3)) * float(rep.get('scale', 0.9))


def _priced(df: pd.DataFrame) -> pd.Series:
    """Rows the pot is spread over: available and inside the draft pool."""
    avail = df['IsAvailable'] == 1
    if 'InDraftPool' in df.columns:
        return avail & (df['InDraftPool'] == 1)
    return avail


def _board_floor(config: Dict[str, Any]) -> int:
    """Configured minimum bid for league board prices."""
    return int(config.get('value_model', {}).get('reconcile', {}).get('min_value', 1))


def _round_and_reconcile(df: pd.DataFrame, prices: pd.Series, target: float,
                         config: Dict[str, Any], output_column: str) -> pd.DataFrame:
    """
    Round prices into integer dollars and reconcile the target with a floor.

    The same remainder distribution is used for FinalAdj and MarketPrice so
    both columns have identical integer reconciliation behavior.
    """
    df = df.copy()
    rec = config.get('value_model', {}).get('reconcile', {})
    min_value = int(rec.get('min_value', 1))

    prices = pd.Series(prices, index=df.index).fillna(0.0)
    df[output_column] = 0
    avail = _priced(df)
    df.loc[avail, output_column] = prices[avail].round().clip(lower=min_value).astype(int)

    if not rec.get('enabled', True) or not avail.any():
        return df

    order = df.loc[avail].sort_values('RawAdj', ascending=False).index.tolist()
    target = int(round(target))

    # Late in a draft the pot can be smaller than $1 x (available players), so a
    # $1 floor on everyone is unaffordable. The cheapest tail is then not
    # rosterable with the money left and is priced at $0 rather than leaving the
    # board unreconciled.
    floors = {idx: min_value for idx in order}
    if target < min_value * len(order):
        affordable = max(0, target // min_value) if min_value > 0 else len(order)
        for i, idx in enumerate(order):
            floors[idx] = min_value if i < affordable else 0
        df.loc[avail, output_column] = [floors[idx] for idx in df.loc[avail].index]

    diff = target - int(df.loc[avail, output_column].sum())
    step = 1 if diff > 0 else -1
    guard = 0
    while diff != 0 and guard < len(order) * 1000:
        for idx in order:
            if diff == 0:
                break
            value = int(df.at[idx, output_column])
            if step < 0 and value <= floors[idx]:
                continue
            df.at[idx, output_column] = value + step
            diff -= step
        guard += len(order)

    if diff != 0:
        raise ValueError(
            f"Could not reconcile board to remaining pot: ${target} target, "
            f"${int(df.loc[avail, output_column].sum())} allocated over {len(order)} "
            f"available players (min value ${min_value}).")
    return df


def reconcile_to_pot(df: pd.DataFrame, remaining_pot: float, config: Dict[str, Any]) -> pd.DataFrame:
    """
    Price surplus above the minimum bid, round to dollars, and reconcile FinalAdj.
    """
    df = df.copy()
    min_bid = _board_floor(config)
    raw = pd.to_numeric(df['RawAdj'], errors='coerce').fillna(0.0)
    surplus = (raw - min_bid).clip(lower=0.0)
    prices = min_bid + df['MarketScalar'] * surplus
    return _round_and_reconcile(df, prices, remaining_pot, config, 'FinalAdj')


def _market_prices(df: pd.DataFrame, remaining_pot: float,
                   config: Dict[str, Any]) -> pd.DataFrame:
    """Solve biased market prices over the configured share of the pot."""
    df = df.copy()
    market = config.get('value_model', {}).get('market', {})
    if not market.get('enabled', False):
        df['MarketPrice'] = 0
        df['Edge'] = df['FinalAdj'] - df['MarketPrice']
        return df

    min_bid = _board_floor(config)
    biases = market.get('position_bias', {})
    bias = df['Position'].map(biases).fillna(1.0)
    market_raw = pd.to_numeric(df['RawAdj'], errors='coerce').fillna(0.0) * bias
    avail = _priced(df)
    target = round(float(remaining_pot) * float(market.get('spend_rate', 1.0)))
    minimum_target = int(avail.sum()) * min_bid
    if float(remaining_pot) >= minimum_target:
        target = max(target, minimum_target)
    target = min(target, int(float(remaining_pot)))
    scalar = solve_for_pot(market_raw.loc[avail], target, min_bid=min_bid)
    prices = min_bid + scalar * (market_raw - min_bid).clip(lower=0.0)
    df = _round_and_reconcile(df, prices, target, config, 'MarketPrice')
    df['Edge'] = (df['FinalAdj'] - df['MarketPrice']).astype(int)
    return df


# ── Ranks, keys, tiers ───────────────────────────────────────────────────────

def assign_pos_rank_and_key(df: pd.DataFrame) -> pd.DataFrame:
    """PosRankByAdj = rank within position by FinalAdj desc. Key = 'POS|rank'."""
    df = df.copy()
    df['PosRankByAdj'] = pd.NA
    avail = df['IsAvailable'] == 1
    ranks = (df.loc[avail].groupby('Position')['FinalAdj']
             .rank(ascending=False, method='first'))
    df.loc[avail, 'PosRankByAdj'] = ranks.astype('Int64')
    df['Key'] = [
        f"{pos}|{rank}" if not pd.isna(rank) else ''
        for pos, rank in zip(df['Position'].fillna(''), df['PosRankByAdj'])
    ]
    return df


def assign_tier(df: pd.DataFrame, config: Dict[str, Any]) -> pd.DataFrame:
    """Tier = first configured bucket whose min_final_adj <= FinalAdj."""
    df = df.copy()
    tiers: List[Dict[str, Any]] = config.get('value_model', {}).get('tiers', [])
    ordered = sorted(tiers, key=lambda t: t.get('min_final_adj', 0), reverse=True)

    def bucket(value, available, tag):
        if available == 1 and float(value) <= 0:
            return 'Undrafted'
        if available != 1:
            # Unavailable rows carry their status instead of a value tier, and a
            # live-draft sale is not a keeper.
            return str(tag).split(' $')[0] or 'Unavailable'
        for tier in ordered:
            if float(value) >= float(tier.get('min_final_adj', 0)):
                return tier.get('name', '')
        return ordered[-1].get('name', '') if ordered else ''

    tags = df['Tag'] if 'Tag' in df.columns else pd.Series([''] * len(df), index=df.index)
    df['Tier'] = [bucket(v, a, t) for v, a, t in zip(df['FinalAdj'], df['IsAvailable'], tags)]
    return df


# ── Orchestration ────────────────────────────────────────────────────────────

def run_value_model(df: pd.DataFrame, remaining_pot: float, config: Dict[str, Any],
                    mode: Optional[str] = None) -> pd.DataFrame:
    """
    Run the full pipeline.

    Args:
        df: rows with at least Player, Position, and per-site baseline columns.
        remaining_pot: league-wide dollars left after keepers.
        config: loaded config.yaml.
        mode: 'pot_solve' (default) or 'replication' (constant 1.3 * 0.9).
    """
    mode = mode or config.get('value_model', {}).get('mode', 'pot_solve')

    df = compute_avg_baseline(df, config)
    df = compute_rank_avail(df)
    df = flag_draft_pool(df, config)
    df = apply_premium_factors(df, config)
    df = compute_raw_adj(df)

    avail = _priced(df)
    if mode == 'replication':
        scalar = replication_scalar(config)
    else:
        min_bid = _board_floor(config)
        scalar = solve_for_pot(df.loc[avail, 'RawAdj'], remaining_pot, min_bid=min_bid)
    df['MarketScalar'] = round(scalar, 6)

    df = reconcile_to_pot(df, remaining_pot, config) if mode != 'replication' else _round_only(df, config)
    df = _market_prices(df, remaining_pot, config)
    df = assign_pos_rank_and_key(df)
    df = assign_tier(df, config)

    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    return df


def _round_only(df: pd.DataFrame, config: Dict[str, Any]) -> pd.DataFrame:
    """Replication mode: round to dollars with the $1 floor, no pot reconciliation."""
    df = df.copy()
    min_value = int(config.get('value_model', {}).get('reconcile', {}).get('min_value', 1))
    scaled = pd.to_numeric(df['RawAdj'], errors='coerce').fillna(0.0) * df['MarketScalar']
    df['FinalAdj'] = 0
    avail = _priced(df)
    df.loc[avail, 'FinalAdj'] = scaled[avail].round().clip(lower=min_value).astype(int)
    return df


def position_sanity_check(df: pd.DataFrame, config: Dict[str, Any],
                          remaining_pot: float) -> pd.DataFrame:
    """
    Optional 2-QB sanity report: summed FinalAdj by position vs. expected spend
    from starting roster slots (10 teams x slots, incl. 2 QB).

    Understated QB dollars here usually means the pull was NOT superflex (`OP`).

    FLEX is part of the slot denominator (so the shares sum to 100%) and is
    reported as its own row: flex dollars are actually spent on RB/WR/TE, so
    those positions are expected to run above their fixed-slot share by roughly
    the flex share.
    """
    league = config.get('league', {})
    teams = league.get('teams', 10)
    all_slots = league.get('roster_slots', {})
    slots = {k: v for k, v in all_slots.items() if k != 'FLEX'}
    total_slots = sum(all_slots.values()) or 1

    avail = df[df['IsAvailable'] == 1]
    actual = avail.groupby('Position')['FinalAdj'].sum()
    rows = []
    for pos, count in slots.items():
        expected_share = (count * teams) / (total_slots * teams)
        rows.append({
            'Position': pos,
            'StartingSlots': count * teams,
            'ActualValue': int(actual.get(pos, 0)),
            'ActualShare': round(float(actual.get(pos, 0)) / remaining_pot, 4) if remaining_pot else 0.0,
            'SlotShare': round(expected_share, 4),
            'ExpectedValueBySlots': int(round(expected_share * remaining_pot)),
        })
    flex = all_slots.get('FLEX')
    if flex:
        flex_share = (flex * teams) / (total_slots * teams)
        rows.append({
            'Position': 'FLEX (spent as RB/WR/TE)',
            'StartingSlots': flex * teams,
            'ActualValue': pd.NA,
            'ActualShare': pd.NA,
            'SlotShare': round(flex_share, 4),
            'ExpectedValueBySlots': int(round(flex_share * remaining_pot)),
        })
    return pd.DataFrame(rows)
