"""
prefetch_cli.py - Daily bulk prefetch (run once per day).

Fills the SQLite cache with the superflex/PPR/dynasty rankings, ADP and the
player universe so build_board.py can run all day with zero API calls.

Cost: ~3 calls (+1 per optional per-position pull) of the 50/day budget.
Re-running within the 24h TTL costs 0 calls.

Usage:
    python prefetch_cli.py
    python prefetch_cli.py --no-per-position
"""

import argparse
import os

from cache import build_cache
from config import load_config
from fantasypros_client import FantasyProsClient
from prefetch import prefetch_all_player_data


def main() -> int:
    parser = argparse.ArgumentParser(description='Daily FantasyPros prefetch.')
    parser.add_argument('--config', default=None)
    parser.add_argument('--no-per-position', action='store_true',
                        help='Skip the QB/RB/WR/TE pulls (saves 4 calls)')
    parser.add_argument('--no-players', action='store_true',
                        help='Skip the player-universe pull (saves 1 call)')
    args = parser.parse_args()

    config = load_config(args.config)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cache = build_cache(config, base_dir)
    client = FantasyProsClient(config=config)

    data = prefetch_all_player_data(
        client, cache, config,
        include_per_position=not args.no_per_position,
        include_players=not args.no_players,
    )

    filters = data['filters']
    print(f"Filters: sport={filters.get('sport')} scoring={filters.get('scoring')} "
          f"position={filters.get('position')} type={filters.get('type')} season={filters.get('season')}")
    for name in ('dynasty', 'adp', 'players'):
        print(f"  {name}: {len(data.get(name, {}).get('players', []))} rows")
    for pos, bundle in data.get('per_position', {}).items():
        print(f"  dynasty[{pos}]: {len(bundle.get('players', []))} rows")

    rl = config.get('rate_limit', {})
    stats = cache.stats(window_seconds=rl.get('window_seconds', 86400))
    print(f"Cache: {stats['valid_entries']}/{stats['total_entries']} valid entries")
    print(f"Rate limit: {stats['rate_limits'].get(rl.get('api_name', 'fantasypros'), {})}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
