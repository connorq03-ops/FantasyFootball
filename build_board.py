"""
build_board.py - End-to-end auction board builder.

Normal path makes ZERO new API calls: it runs entirely off the SQLite cache
populated by the daily `prefetch.py` run (24h TTL, 50 calls/day budget).

Usage:
    python prefetch_cli.py                  # once per day: fill the cache
    python build_board.py                   # build the board off cache
    python build_board.py --sold sold.csv   # live draft mode
    python build_board.py --mode replication
    python build_board.py --allow-api-calls # permit a cache-miss refresh
"""

import argparse
import os
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from cache import build_cache
from config import load_config, resolve_path
from fantasypros_client import FantasyProsClient
from names import build_index, match_name, normalized_key
from prefetch import prefetch_all_player_data, rankings_to_rows
from value_model import (
    REQUIRED_COLUMNS,
    position_sanity_check,
    rank_to_baseline,
    run_value_model,
)

OUTPUT_COLUMNS = REQUIRED_COLUMNS + ['Manager', 'KeeperCost', 'KeeperYear', 'FP_RankEcr', 'FP_Adp']


# ── Inputs ───────────────────────────────────────────────────────────────────

def load_keepers(path: str) -> pd.DataFrame:
    """Load keepers.csv (Team, Manager, Player, KeeperCost, KeeperYear)."""
    df = pd.read_csv(path)
    df['KeeperCost'] = pd.to_numeric(df['KeeperCost'], errors='coerce').fillna(0).astype(int)
    df['NameKey'] = df['Player'].apply(normalized_key)
    return df


def compute_team_budgets(keepers: pd.DataFrame, team_budgets_path: Optional[str],
                         starting_budget: int) -> pd.DataFrame:
    """
    Recompute per-team budgets from keepers.csv rather than trusting the seed CSV.

    AvailableBudget = StartingBudget - team keeper spend. It is that team's
    max-spend CAP, not a re-pricing input: the board is one shared market
    priced to the league-wide remaining pot.
    """
    seed = pd.read_csv(team_budgets_path) if team_budgets_path and os.path.exists(team_budgets_path) else None

    spend = (keepers.groupby(['Team', 'Manager'], as_index=False)['KeeperCost']
             .sum().rename(columns={'KeeperCost': 'KeeperSpend'}))

    if seed is not None:
        seed = seed.copy()
        seed['StartingBudget'] = pd.to_numeric(seed.get('StartingBudget'), errors='coerce').fillna(starting_budget)
        budgets = seed[['Team', 'Manager', 'StartingBudget']].merge(spend, on=['Team', 'Manager'], how='outer')
    else:
        budgets = spend.copy()
        budgets['StartingBudget'] = starting_budget

    budgets['StartingBudget'] = budgets['StartingBudget'].fillna(starting_budget).astype(int)
    budgets['KeeperSpend'] = budgets['KeeperSpend'].fillna(0).astype(int)
    budgets['AvailableBudget'] = budgets['StartingBudget'] - budgets['KeeperSpend']
    return budgets.sort_values('Team').reset_index(drop=True)


def load_espn_baselines(path: Optional[str]) -> Optional[pd.DataFrame]:
    """Optional espn_baselines.csv (Player, ESPN_Baseline [, Position, Team])."""
    if not path or not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df['NameKey'] = df['Player'].apply(normalized_key)
    return df


def load_sold(path: Optional[str]) -> Optional[pd.DataFrame]:
    """Optional sold.csv for live draft mode (Player, SoldPrice, WinningTeam)."""
    if not path or not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df['SoldPrice'] = pd.to_numeric(df['SoldPrice'], errors='coerce').fillna(0).astype(int)
    df['NameKey'] = df['Player'].apply(normalized_key)
    return df


# ── Board assembly ───────────────────────────────────────────────────────────

def build_player_frame(prefetched: Dict[str, Any], keepers: pd.DataFrame,
                       espn: Optional[pd.DataFrame], config: Dict[str, Any]) -> pd.DataFrame:
    """Merge FantasyPros rows, keeper flags and ESPN baselines into board rows."""
    rows = rankings_to_rows(prefetched)
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=['Player', 'Position', 'Team', 'Bye'])
    df['NameKey'] = df['Player'].apply(normalized_key)
    df['FP_Baseline'] = rank_to_baseline(pd.to_numeric(df.get('FP_RankEcr'), errors='coerce'), config)

    # Keepers that FantasyPros didn't return still belong on the board (as
    # unavailable rows) so keeper spend and the pot stay auditable.
    fp_index = build_index(df['Player'].tolist())
    keeper_rows = []
    for _, k in keepers.iterrows():
        matched, _score = match_name(k['Player'], fp_index)
        if matched is None:
            keeper_rows.append({'Player': k['Player'], 'Position': '', 'Team': '', 'Bye': '',
                                'NameKey': k['NameKey'], 'FP_Baseline': pd.NA})
    if keeper_rows:
        df = pd.concat([df, pd.DataFrame(keeper_rows)], ignore_index=True)

    keeper_map = {}
    for _, k in keepers.iterrows():
        matched, _score = match_name(k['Player'], build_index(df['Player'].tolist()))
        key = normalized_key(matched) if matched else k['NameKey']
        keeper_map[key] = k

    df['IsAvailable'] = [0 if key in keeper_map else 1 for key in df['NameKey']]
    df['Tag'] = ['Keeper' if key in keeper_map else '' for key in df['NameKey']]
    df['Manager'] = [keeper_map[key]['Manager'] if key in keeper_map else '' for key in df['NameKey']]
    df['KeeperCost'] = [keeper_map[key]['KeeperCost'] if key in keeper_map else pd.NA for key in df['NameKey']]
    df['KeeperYear'] = [keeper_map[key]['KeeperYear'] if key in keeper_map else '' for key in df['NameKey']]

    df['ESPN_Baseline'] = pd.NA
    if espn is not None and not espn.empty:
        board_index = build_index(df['Player'].tolist())
        espn_by_key = {}
        for _, row in espn.iterrows():
            matched, _score = match_name(row['Player'], board_index)
            espn_by_key[normalized_key(matched) if matched else row['NameKey']] = row['ESPN_Baseline']
        df['ESPN_Baseline'] = [espn_by_key.get(key, pd.NA) for key in df['NameKey']]

    return df


def apply_sold(df: pd.DataFrame, sold: pd.DataFrame, budgets: pd.DataFrame,
               remaining_pot: int) -> Tuple[pd.DataFrame, pd.DataFrame, int]:
    """
    Live draft mode: mark sold players unavailable, subtract their price from the
    league-wide pot and from the winning team's available budget, so the
    remaining board can be re-solved against the true remaining money.
    """
    df = df.copy()
    budgets = budgets.copy()
    board_index = build_index(df['Player'].tolist())

    for _, row in sold.iterrows():
        matched, _score = match_name(row['Player'], board_index)
        key = normalized_key(matched) if matched else row['NameKey']
        mask = df['NameKey'] == key
        if mask.any():
            df.loc[mask, 'IsAvailable'] = 0
            df.loc[mask, 'Tag'] = f"Sold ${int(row['SoldPrice'])} ({row['WinningTeam']})"
        else:
            df = pd.concat([df, pd.DataFrame([{
                'Player': row['Player'], 'NameKey': key, 'IsAvailable': 0,
                'Tag': f"Sold ${int(row['SoldPrice'])} ({row['WinningTeam']})",
            }])], ignore_index=True)

        remaining_pot -= int(row['SoldPrice'])
        winner = str(row['WinningTeam'])
        won = (budgets['Team'].astype(str) == winner) | (budgets['Manager'].astype(str) == winner)
        budgets.loc[won, 'AvailableBudget'] -= int(row['SoldPrice'])

    return df, budgets, remaining_pot


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description='Build the dynasty auction board.')
    parser.add_argument('--config', default=None, help='Path to config.yaml')
    parser.add_argument('--mode', choices=['pot_solve', 'replication'], default=None,
                        help='Value mode (default: config value_model.mode)')
    parser.add_argument('--sold', default=None, help='sold.csv for live draft mode')
    parser.add_argument('--espn', default=None, help='espn_baselines.csv override')
    parser.add_argument('--output-dir', default=None, help='Output directory')
    parser.add_argument('--allow-api-calls', action='store_true',
                        help='Permit live API calls on cache misses (counts against 50/day)')
    args = parser.parse_args()

    config = load_config(args.config)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cache = build_cache(config, base_dir)

    client = None
    if args.allow_api_calls:
        client = FantasyProsClient(config=config)
    else:
        class _CachedOnlyClient:
            """Fails loudly instead of spending the daily budget."""

            def __getattr__(self, name):
                def _blocked(*_a, **_kw):
                    raise RuntimeError(
                        f"build_board.py tried a live API call ({name}). Run "
                        f"`python prefetch_cli.py` first, or pass --allow-api-calls."
                    )
                return _blocked

        client = _CachedOnlyClient()

    prefetched = prefetch_all_player_data(client, cache, config)

    keepers = load_keepers(resolve_path(config, 'keepers_csv', base_dir))
    starting_budget = config.get('league', {}).get('starting_budget', 200)
    budgets = compute_team_budgets(keepers, resolve_path(config, 'team_budgets_csv', base_dir),
                                  starting_budget)
    espn = load_espn_baselines(args.espn or resolve_path(config, 'espn_baselines_csv', base_dir))

    df = build_player_frame(prefetched, keepers, espn, config)

    remaining_pot = int(budgets['AvailableBudget'].sum())
    sold = load_sold(args.sold or (resolve_path(config, 'sold_csv', base_dir) if args.sold else None))
    if sold is not None and not sold.empty:
        df, budgets, remaining_pot = apply_sold(df, sold, budgets, remaining_pot)

    df = run_value_model(df, remaining_pot, config, mode=args.mode)
    df = df.sort_values(['IsAvailable', 'FinalAdj'], ascending=[False, False])

    output_dir = args.output_dir or resolve_path(config, 'output_dir', base_dir)
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    board_path = os.path.join(output_dir, f'board_{stamp}.csv')
    out = df[[c for c in OUTPUT_COLUMNS if c in df.columns]].copy()
    for col in ('FP_Baseline', 'ESPN_Baseline', 'Avg_Baseline', 'RawAdj'):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors='coerce').round(2)
    out.to_csv(board_path, index=False)

    budget_path = os.path.join(output_dir, f'team_budgets_{stamp}.csv')
    budgets[['Manager', 'Team', 'StartingBudget', 'KeeperSpend', 'AvailableBudget']].to_csv(
        budget_path, index=False)

    avail = df[df['IsAvailable'] == 1]
    print(f"Players on board: {len(df)} ({len(avail)} available)")
    print(f"Remaining pot: ${remaining_pot} | MarketScalar: {df['MarketScalar'].iloc[0]:.4f}")
    print(f"Sum(FinalAdj) over available: ${int(avail['FinalAdj'].sum())}")
    print(f"Board:   {board_path}")
    print(f"Budgets: {budget_path}")

    if config.get('value_model', {}).get('position_sanity_check', True) and len(avail):
        report = position_sanity_check(df, config, remaining_pot)
        sanity_path = os.path.join(output_dir, f'position_sanity_{stamp}.csv')
        report.to_csv(sanity_path, index=False)
        print("\n2-QB position sanity check (superflex baselines expected to fund QBs):")
        print(report.to_string(index=False))

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
