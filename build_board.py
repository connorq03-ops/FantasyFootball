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

from auction_values import compute_auction_values, projection_rows
from cache import build_cache
from config import load_config, resolve_path
from fantasypros_client import FantasyProsClient
from names import build_index, match_name, normalized_key
from prefetch import prefetch_all_player_data, rankings_to_rows
from value_model import (
    REQUIRED_COLUMNS,
    position_sanity_check,
    run_value_model,
)

BASELINE_COLUMNS = ['DS_MarketValue']

# Every source baseline sits next to FP_Baseline so the sites can be eyeballed
# side by side; the derived/league columns follow, then the reference data.
OUTPUT_COLUMNS = (
    REQUIRED_COLUMNS[:REQUIRED_COLUMNS.index('FP_Baseline') + 1]
    + BASELINE_COLUMNS
    + REQUIRED_COLUMNS[REQUIRED_COLUMNS.index('FP_Baseline') + 1:]
    + ['Manager', 'KeeperCost', 'KeeperYear', 'FP_Points', 'FP_Vorp', 'FP_RankEcr', 'FP_Adp']
)


# ── Inputs ───────────────────────────────────────────────────────────────────

def parse_dollars(series: pd.Series, column: str, players: pd.Series) -> pd.Series:
    """
    Parse a whole-dollar money column, raising on anything that would silently
    distort budgets (blank, non-numeric, fractional or negative values).
    """
    values = pd.to_numeric(series, errors='coerce')
    bad = [f"{player!r}={raw!r}" for player, raw, value in zip(players, series, values)
           if pd.isna(value) or value < 0 or float(value) != int(value)]
    if bad:
        raise ValueError(
            f"{column} must be a whole, non-negative dollar amount; got {', '.join(bad)}")
    return values.astype(int)


def load_keepers(path: str) -> pd.DataFrame:
    """Load keepers.csv (Team, Manager, Player, KeeperCost, KeeperYear)."""
    df = pd.read_csv(path)
    df['KeeperCost'] = parse_dollars(df['KeeperCost'], 'keepers.csv KeeperCost', df['Player'])
    df['NameKey'] = df['Player'].apply(normalized_key)
    return df


def compute_team_budgets(keepers: pd.DataFrame, team_budgets_path: Optional[str],
                         starting_budget: int) -> pd.DataFrame:
    """
    Recompute per-team budgets from keepers.csv rather than trusting the seed CSV.

    AvailableBudget = StartingBudget - team keeper spend. It is that team's
    max-spend CAP, not a re-pricing input: the board is one shared market
    priced to the league-wide remaining pot.

    Team is the budget identity: joining on (Team, Manager) would silently
    create two budget rows - and an extra starting budget in the pot - if a
    manager's name is spelled differently across the two CSVs.
    """
    seed = pd.read_csv(team_budgets_path) if team_budgets_path and os.path.exists(team_budgets_path) else None

    spend = (keepers.groupby('Team', as_index=False)
             .agg(Manager=('Manager', 'first'), KeeperSpend=('KeeperCost', 'sum')))

    if seed is not None:
        seed = seed.copy()
        dupes = seed['Team'][seed['Team'].duplicated()].unique().tolist()
        if dupes:
            raise ValueError(f"Duplicate Team rows in team_budgets.csv: {dupes}")
        # A blank StartingBudget keeps the configured default; anything supplied
        # gets the same whole-dollar validation as keeper costs and sold prices.
        supplied = seed['StartingBudget'] if 'StartingBudget' in seed.columns else pd.Series(dtype=object)
        given = supplied.notna() & (supplied.astype(str).str.strip() != '')
        if given.any():
            parse_dollars(supplied[given], 'team_budgets.csv StartingBudget', seed['Team'][given])
        seed['StartingBudget'] = pd.to_numeric(supplied, errors='coerce').fillna(starting_budget)
        budgets = seed[['Team', 'Manager', 'StartingBudget']].merge(
            spend[['Team', 'KeeperSpend']], on='Team', how='outer')
        budgets['Manager'] = budgets['Manager'].fillna(
            budgets['Team'].map(dict(zip(spend['Team'], spend['Manager']))))
    else:
        budgets = spend.copy()
        budgets['StartingBudget'] = starting_budget

    budgets['StartingBudget'] = budgets['StartingBudget'].fillna(starting_budget).astype(int)
    budgets['KeeperSpend'] = budgets['KeeperSpend'].fillna(0).astype(int)
    budgets['AvailableBudget'] = budgets['StartingBudget'] - budgets['KeeperSpend']
    over = budgets[budgets['AvailableBudget'] < 0]
    if not over.empty:
        raise ValueError(
            "keepers.csv overspends the starting budget: " + ', '.join(
                f"{team} at ${budget}" for team, budget
                in zip(over['Team'], over['AvailableBudget'])))
    return budgets.sort_values('Team').reset_index(drop=True)


def load_espn_baselines(path: Optional[str]) -> Optional[pd.DataFrame]:
    """Optional espn_baselines.csv (Player, ESPN_Baseline [, Position, Team])."""
    if not path or not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df['NameKey'] = df['Player'].apply(normalized_key)
    return df


def load_fp_auction(path: Optional[str]) -> Optional[pd.DataFrame]:
    """Optional fp_auction_values.csv, written by `python fp_auction.py`."""
    if not path or not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df['NameKey'] = df['Player'].apply(normalized_key)
    return df


def load_draftsharks(path: Optional[str]) -> Optional[pd.DataFrame]:
    """Optional draftsharks.csv (Player, Position, DS_Baseline, DS_MarketValue)."""
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
    df['SoldPrice'] = parse_dollars(df['SoldPrice'], 'sold.csv SoldPrice', df['Player'])
    df['NameKey'] = df['Player'].apply(normalized_key)
    return df


# ── Board assembly ───────────────────────────────────────────────────────────

def fp_auction_baselines(prefetched: Dict[str, Any], config: Dict[str, Any],
                         published: Optional[pd.DataFrame] = None,
                         ) -> Dict[str, Dict[str, float]]:
    """
    FantasyPros source dollars, keyed by normalized name.

    Preferred source is FantasyPros' own auction calculator, refreshed into
    `fp_auction_values.csv` by `python fp_auction.py`. When that file is absent
    the dollars are reconstructed from the projections pull (auction_values.py)
    so a board can still be built offline.

    Either way these are absolute values for the reference format in
    `baseline_auction` (superflex, PPR, $200 x 10) and are never rescaled by
    this league's keepers, remaining pot or draft state — those only move
    RawAdj/FinalAdj downstream.
    """
    if published is not None and not published.empty:
        return {
            row['NameKey']: {'AuctionValue': float(row['FP_Baseline']),
                             'Points': float(row['FP_Points'])}
            for _, row in published.iterrows()
        }
    payload = prefetched.get('projections', {}).get('raw')
    if not payload:
        return {}
    values = compute_auction_values(projection_rows(payload), config)
    return {normalized_key(row['Player']): row for row in values if row.get('Player')}


def build_player_frame(prefetched: Dict[str, Any], keepers: pd.DataFrame,
                       espn: Optional[pd.DataFrame], config: Dict[str, Any],
                       draftsharks: Optional[pd.DataFrame] = None,
                       fp_auction: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Merge FantasyPros rows, keeper flags and external baselines into board rows."""
    rows = rankings_to_rows(prefetched)
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=['Player', 'Position', 'Team', 'Bye'])
    df['NameKey'] = df['Player'].apply(normalized_key)

    auction = fp_auction_baselines(prefetched, config, fp_auction)
    # Players the source does not price are $0, not missing: outside the
    # rosterable pool of the reference format they are not auction assets.
    df['FP_Baseline'] = [auction.get(key, {}).get('AuctionValue', 0.0) if auction else pd.NA
                         for key in df['NameKey']]
    df['FP_Points'] = [auction.get(key, {}).get('Points', pd.NA) for key in df['NameKey']]
    df['FP_Vorp'] = [auction.get(key, {}).get('Vorp', pd.NA) for key in df['NameKey']]

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

    df['DS_MarketValue'] = pd.NA
    if draftsharks is not None and not draftsharks.empty:
        board_index = build_index(df['Player'].tolist())
        ds_by_key: Dict[str, pd.Series] = {}
        for _, row in draftsharks.iterrows():
            matched, _score = match_name(row['Player'], board_index)
            ds_by_key[normalized_key(matched) if matched else row['NameKey']] = row
        df['DS_MarketValue'] = [ds_by_key[key]['DS_MarketValue'] if key in ds_by_key else pd.NA
                                for key in df['NameKey']]

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

    # The whole batch is validated before anything is mutated: a bad row would
    # otherwise debit the pot (or a team twice) while leaving the board wrong.
    resolved = []
    seen_keys = {}
    for _, row in sold.iterrows():
        player = row['Player']
        price = int(row['SoldPrice'])

        matched, _score = match_name(player, board_index)
        key = normalized_key(matched) if matched else None
        rows = df.index[df['NameKey'] == key].tolist() if key else []
        if not rows:
            raise ValueError(
                f"sold.csv player {player!r} is not on the board; fix the spelling or add "
                f"an override in names.py rather than pricing a phantom player.")
        if key in seen_keys:
            raise ValueError(f"sold.csv lists {player!r} twice (also as {seen_keys[key]!r}).")
        seen_keys[key] = player
        if not (df.loc[rows, 'IsAvailable'] == 1).all():
            raise ValueError(
                f"sold.csv player {player!r} is already unavailable (keeper or earlier sale).")

        winner = str(row['WinningTeam'])
        won = (budgets['Team'].astype(str) == winner) | (budgets['Manager'].astype(str) == winner)
        if int(won.sum()) != 1:
            raise ValueError(
                f"sold.csv WinningTeam {winner!r} (player {player!r}) matched {int(won.sum())} "
                f"teams; expected exactly one Team or Manager in team_budgets.csv.")
        resolved.append((rows, price, winner, won))

    for rows, price, winner, won in resolved:
        df.loc[rows, 'IsAvailable'] = 0
        df.loc[rows, 'Tag'] = f"Sold ${price} ({winner})"
        remaining_pot -= price
        budgets.loc[won, 'AvailableBudget'] -= price

    over = budgets[budgets['AvailableBudget'] < 0]
    if not over.empty:
        raise ValueError(
            "sold.csv overspends: " + ', '.join(
                f"{team} at ${budget}" for team, budget
                in zip(over['Team'], over['AvailableBudget'])))
    if remaining_pot < 0:
        raise ValueError(f"sold.csv spends more than the league pot (remaining ${remaining_pot}).")

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
                        help='Permit live API calls on cache misses (counts against the daily budget)')
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

    # cache_only: a run that cannot make live calls must not reserve daily
    # budget for a lookup it will never perform.
    # optional_ok: only the superflex dynasty pull prices the board, so a cache
    # written by a reduced prefetch (--no-players / --no-per-position) still works.
    prefetched = prefetch_all_player_data(client, cache, config, optional_ok=True,
                                          cache_only=not args.allow_api_calls)

    keepers = load_keepers(resolve_path(config, 'keepers_csv', base_dir))
    starting_budget = config.get('league', {}).get('starting_budget', 200)
    budgets = compute_team_budgets(keepers, resolve_path(config, 'team_budgets_csv', base_dir),
                                  starting_budget)
    espn = load_espn_baselines(args.espn or resolve_path(config, 'espn_baselines_csv', base_dir))
    draftsharks = load_draftsharks(resolve_path(config, 'draftsharks_csv', base_dir))
    fp_auction = load_fp_auction(resolve_path(config, 'fp_auction_csv', base_dir))

    df = build_player_frame(prefetched, keepers, espn, config, draftsharks, fp_auction)

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
    for col in ('FP_Baseline', 'ESPN_Baseline', 'DS_MarketValue',
                'FP_Points', 'FP_Vorp', 'Avg_Baseline', 'RawAdj',
                'MarketPrice', 'Edge'):
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
    print(f"Sum(MarketPrice) over available: ${int(avail['MarketPrice'].sum())}")
    top_edges = avail[avail['Edge'] > 0].nlargest(5, 'Edge')[['Player', 'Edge']]
    if top_edges.empty:
        print("Top positive Edge: none")
    else:
        print("Top positive Edge:")
        print(top_edges.to_string(index=False))
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
