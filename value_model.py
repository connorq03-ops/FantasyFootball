"""
value_model.py - Auction value pipeline for a 10-team, 2-QB dynasty/keeper league.

Columns produced per player (mirroring the league spreadsheet):
    Player, Position, Team, Bye, FP_Baseline, ESPN_Baseline, Tag, IsAvailable,
    Avg_Baseline, RankAvail, PremiumFactor, LowValueFactor, RawAdj,
    MarketScalar, FinalAdj, PosRankByAdj, Key, Tier

Pipeline (each step is a composable function operating on a pandas DataFrame):
    compute_avg_baseline -> compute_rank_avail -> apply_premium_factors ->
    compute_raw_adj -> solve_for_pot -> reconcile_to_pot ->
    assign_pos_rank_and_key -> assign_tier

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
    'IsAvailable', 'Avg_Baseline', 'RankAvail', 'PremiumFactor', 'LowValueFactor',
    'RawAdj', 'MarketScalar', 'FinalAdj', 'PosRankByAdj', 'Key', 'Tier',
]


# ── Baselines ────────────────────────────────────────────────────────────────

def rank_to_baseline(ranks: pd.Series, config: Dict[str, Any]) -> pd.Series:
    """
    Convert superflex ECR ranks into a value baseline (higher = more valuable).

    FantasyPros publishes ranks, not dollars, so the board needs a monotonic
    rank->value curve before averaging with dollar-denominated sites. The curve
    is exponential decay: top_value * exp(-(rank - 1) / decay), floored at
    min_value. Knobs live in config.yaml (`value_model.fp_baseline_from_rank`).
    """
    cfg = config.get('value_model', {}).get('fp_baseline_from_rank', {})
    top_value = cfg.get('top_value', 60.0)
    decay = cfg.get('decay', 25.0)
    min_value = cfg.get('min_value', 1.0)

    def convert(rank):
        if pd.isna(rank):
            return math.nan
        return max(min_value, top_value * math.exp(-(float(rank) - 1.0) / decay))

    return ranks.apply(convert)


def compute_avg_baseline(df: pd.DataFrame, config: Dict[str, Any]) -> pd.DataFrame:
    """
    Avg_Baseline = mean of the per-site baseline columns, ignoring missing sites.

    Add more sites to `value_model.baseline_columns` in config.yaml and they are
    averaged automatically. Example: FP 34, ESPN 46 -> 40.0.
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


# ── Premium / haircut ────────────────────────────────────────────────────────

def scarcity_premium(rank: Optional[float], position: str, config: Dict[str, Any]) -> float:
    """
    Smooth, tunable scarcity premium as a function of RankAvail.

        premium = 1 + (peak - 1) * exp(-(rank - 1) / decay)      rank <= tail_start
        premium = tail_factor                                    rank >  tail_start

    Defaults are seeded from the spreadsheet's observed values (top overall
    ~1.4, next tier ~1.2-1.25, most 1.0, a deep tail at 0.9).

    `position_multipliers` default to 1.0 and exist only for genuine pool
    scarcity tweaks — do NOT use them to add a QB premium on superflex data.
    """
    cfg = config.get('value_model', {}).get('premium', {})
    if rank is None or pd.isna(rank):
        return 1.0
    rank = float(rank)
    tail_start = cfg.get('tail_start_rank', 36)
    if rank > tail_start:
        premium = cfg.get('tail_factor', 0.90)
    else:
        peak = cfg.get('peak', 1.40)
        decay = cfg.get('decay', 6.0)
        premium = 1.0 + (peak - 1.0) * math.exp(-(rank - 1.0) / decay)
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
    return df


# ── Pot solving ──────────────────────────────────────────────────────────────

def solve_for_pot(raw_adj_values, remaining_pot: float) -> float:
    """
    MarketScalar such that sum(RawAdj * MarketScalar) == remaining_pot.

    This single solved scalar replaces the spreadsheet's separate constant
    InflationFactor (1.3) and Scale (0.9), which were mathematically redundant
    global multipliers.
    """
    total = float(pd.Series(list(raw_adj_values)).fillna(0.0).sum())
    if total <= 0:
        return 0.0
    return float(remaining_pot) / total


def replication_scalar(config: Dict[str, Any]) -> float:
    """Faithful-replication mode: the old constant InflationFactor * Scale."""
    rep = config.get('value_model', {}).get('replication', {})
    return float(rep.get('inflation_factor', 1.3)) * float(rep.get('scale', 0.9))


def reconcile_to_pot(df: pd.DataFrame, remaining_pot: float, config: Dict[str, Any]) -> pd.DataFrame:
    """
    Round RawAdj * MarketScalar into integer dollars with a $1 floor, then
    distribute the leftover rounding remainder to the top players so
    sum(FinalAdj over available players) == remaining_pot exactly.
    """
    df = df.copy()
    rec = config.get('value_model', {}).get('reconcile', {})
    min_value = int(rec.get('min_value', 1))

    scaled = pd.to_numeric(df['RawAdj'], errors='coerce').fillna(0.0) * df['MarketScalar']
    df['FinalAdj'] = 0
    avail = df['IsAvailable'] == 1
    df.loc[avail, 'FinalAdj'] = scaled[avail].round().clip(lower=min_value).astype(int)

    if not rec.get('enabled', True) or not avail.any():
        return df

    order = df.loc[avail].sort_values('RawAdj', ascending=False).index.tolist()
    diff = int(round(remaining_pot)) - int(df.loc[avail, 'FinalAdj'].sum())
    step = 1 if diff > 0 else -1
    guard = 0
    while diff != 0 and guard < len(order) * 1000:
        for idx in order:
            if diff == 0:
                break
            value = int(df.at[idx, 'FinalAdj'])
            if step < 0 and value <= min_value:
                continue
            df.at[idx, 'FinalAdj'] = value + step
            diff -= step
        guard += len(order)
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

    def bucket(value, available):
        if available != 1:
            return 'Keeper'
        for tier in ordered:
            if float(value) >= float(tier.get('min_final_adj', 0)):
                return tier.get('name', '')
        return ordered[-1].get('name', '') if ordered else ''

    df['Tier'] = [bucket(v, a) for v, a in zip(df['FinalAdj'], df['IsAvailable'])]
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
    df = apply_premium_factors(df, config)
    df = compute_raw_adj(df)

    avail = df['IsAvailable'] == 1
    if mode == 'replication':
        scalar = replication_scalar(config)
    else:
        scalar = solve_for_pot(df.loc[avail, 'RawAdj'], remaining_pot)
    df['MarketScalar'] = round(scalar, 6)

    df = reconcile_to_pot(df, remaining_pot, config) if mode != 'replication' else _round_only(df, config)
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
    avail = df['IsAvailable'] == 1
    df.loc[avail, 'FinalAdj'] = scaled[avail].round().clip(lower=min_value).astype(int)
    return df


def position_sanity_check(df: pd.DataFrame, config: Dict[str, Any],
                          remaining_pot: float) -> pd.DataFrame:
    """
    Optional 2-QB sanity report: summed FinalAdj by position vs. expected spend
    from starting roster slots (10 teams x slots, incl. 2 QB).

    Understated QB dollars here usually means the pull was NOT superflex (`OP`).
    """
    league = config.get('league', {})
    teams = league.get('teams', 10)
    slots = {k: v for k, v in league.get('roster_slots', {}).items() if k != 'FLEX'}
    total_slots = sum(slots.values()) or 1

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
    return pd.DataFrame(rows)
