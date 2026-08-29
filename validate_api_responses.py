"""
validate_api_responses.py - Validate real FantasyPros responses against code assumptions.

Modeled on golf/validate_api_responses.py in NCAAProjectCH. Hits each endpoint
with the LEAGUE filters (PPR / superflex `OP` / dynasty / current season), logs
the real JSON structure, and diffs it against what fantasypros_client.py,
prefetch.py and build_board.py expect.

WARNING: this script makes LIVE calls and therefore consumes the daily API
budget (500/day premium, 50/day free). Run it sparingly — normally only after
FantasyPros changes an endpoint or at the start of a new season.

Usage:
    python validate_api_responses.py
    python validate_api_responses.py --skip-per-position   # cheaper run

Requires FANTASYPROS_API_KEY (org secret).
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
load_dotenv()

from config import load_config  # noqa: E402
from fantasypros_client import API_KEY_ENV_VARS, FantasyProsClient  # noqa: E402

# ═══════════════════════════════════════════════════════════════
# Configuration: what the code currently expects
# ═══════════════════════════════════════════════════════════════

EXPECTED: Dict[str, Dict[str, Any]] = {
    'get_dynasty_rankings': {
        'wrapper_keys': ['players'],
        'meta_fields': {
            'sport': str,
            'year': (str, int),
            'position_id': str,
            'scoring': str,
            'ranking_type_name': str,
            'count': int,
        },
        'item_fields': {
            'player_id': (int, str),
            'player_name': str,
            'player_position_id': str,
            'player_team_id': str,
            'player_bye_week': (str, int),
            'rank_ecr': (int, float),
            'rank_ave': (str, int, float),
            'pos_rank': str,
        },
        'code_refs': [
            'prefetch.py:rankings_to_rows() — Player/Position/Team/Bye/FP_RankEcr',
            'value_model.py:rank_to_baseline() — FP_Baseline from rank_ecr',
        ],
        'filters_expected': {'position_id': 'OP', 'scoring': 'PPR', 'ranking_type_name': 'dynasty'},
    },
    'get_consensus_rankings': {
        'wrapper_keys': ['players'],
        'meta_fields': {'sport': str, 'ranking_type_name': str, 'count': int},
        'item_fields': {'player_id': (int, str), 'player_name': str, 'rank_ecr': (int, float)},
        'code_refs': ['fantasypros_client.py:get_consensus_rankings()'],
        'filters_expected': {'position_id': 'OP', 'scoring': 'PPR'},
    },
    'get_adp': {
        'wrapper_keys': ['players'],
        'meta_fields': {'sport': str, 'ranking_type_name': str},
        'item_fields': {'player_id': (int, str), 'player_name': str},
        'code_refs': ['prefetch.py:rankings_to_rows() — FP_Adp'],
        'filters_expected': {'ranking_type_name': 'adp'},
        'allow_empty': True,  # free API tier returns an empty players list
    },
    'get_players': {
        'wrapper_keys': ['players'],
        'meta_fields': {'sport': str, 'count': int, 'season': (str, int)},
        'item_fields': {'player_id': (int, str), 'player_name': str,
                        'position_id': str, 'team_id': str},
        'code_refs': ['prefetch.py:prefetch_all_player_data() — id/metadata universe'],
    },
}


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def describe(value: Any, depth: int = 0) -> str:
    if isinstance(value, dict):
        keys = list(value.keys())
        return f"dict({len(keys)} keys: {keys[:12]}{'...' if len(keys) > 12 else ''})"
    if isinstance(value, list):
        inner = describe(value[0], depth + 1) if value else 'empty'
        return f"list[{len(value)}] of {inner}"
    return type(value).__name__


def check_fields(items: List[Dict[str, Any]], expected: Dict[str, Any]) -> List[str]:
    problems = []
    if not items:
        return ['players list is EMPTY']
    sample = items[0]
    for field, types in expected.items():
        if field not in sample:
            problems.append(f"missing field '{field}' (expected {types})")
        elif sample[field] is not None and not isinstance(sample[field], types):
            problems.append(f"field '{field}' is {type(sample[field]).__name__}, expected {types}")
    return problems


def validate_endpoint(name: str, payload: Any, spec: Dict[str, Any]) -> Tuple[bool, List[str]]:
    problems: List[str] = []
    if not isinstance(payload, dict):
        return False, [f"response is {describe(payload)}, expected dict"]

    print(f"  meta: { {k: v for k, v in payload.items() if k != 'players'} }")

    for key in spec.get('wrapper_keys', []):
        if key not in payload:
            problems.append(f"missing wrapper key '{key}'")

    for field, types in spec.get('meta_fields', {}).items():
        if field not in payload:
            problems.append(f"missing meta field '{field}'")
        elif payload[field] is not None and not isinstance(payload[field], types):
            problems.append(f"meta '{field}' is {type(payload[field]).__name__}, expected {types}")

    for field, want in spec.get('filters_expected', {}).items():
        got = str(payload.get(field, '')).lower()
        if want.lower() not in got:
            problems.append(
                f"LEAGUE FILTER MISMATCH: response '{field}'={payload.get(field)!r}, expected {want!r}")

    items = payload.get('players') or []
    print(f"  players: {describe(items)}")
    if items:
        print(f"  sample: {json.dumps(items[0], indent=2)[:900]}")
        problems += check_fields(items, spec.get('item_fields', {}))
    elif not spec.get('allow_empty'):
        problems.append('players list is EMPTY')
    else:
        print('  players list empty (tolerated: free tier returns no ADP rows)')

    if payload.get('public_api_limited'):
        print(f"  NOTE: API tier '{payload.get('tier')}' truncates responses to "
              f"limit={payload.get('limit')} while count={payload.get('count')}")

    return not problems, problems


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description='Validate live FantasyPros responses.')
    parser.add_argument('--skip-per-position', action='store_true',
                        help='Skip the QB/RB/WR/TE pulls (saves 4 live calls)')
    args = parser.parse_args()

    if not any(os.getenv(v) for v in API_KEY_ENV_VARS):
        print(f"ERROR: set {API_KEY_ENV_VARS[0]} (org secret) before running.")
        return 1

    config = load_config()
    filters = config.get('api_filters', {})
    print('═' * 70)
    print('FantasyPros API validation — LIVE CALLS (consumes the daily budget)')
    print(f"Filters: sport={filters.get('sport')} scoring={filters.get('scoring')} "
          f"position={filters.get('position')} type={filters.get('type')} "
          f"season={filters.get('season')}")
    print('═' * 70)

    client = FantasyProsClient(config=config)
    calls = {
        'get_dynasty_rankings': lambda: client.get_dynasty_rankings(),
        'get_consensus_rankings': lambda: client.get_consensus_rankings(),
        'get_adp': lambda: client.get_adp(),
        'get_players': lambda: client.get_players(),
    }

    failures: Dict[str, List[str]] = {}
    for name, call in calls.items():
        print(f"\n── {name} ──")
        print(f"  code refs: {EXPECTED[name].get('code_refs')}")
        try:
            payload = call()
        except Exception as e:
            failures[name] = [f"request failed: {type(e).__name__}: {e}"]
            print(f"  FAILED: {type(e).__name__}: {e}")
            continue
        ok, problems = validate_endpoint(name, payload, EXPECTED[name])
        if ok:
            print('  OK — matches code assumptions')
        else:
            failures[name] = problems
            for p in problems:
                print(f"  MISMATCH: {p}")

    if not args.skip_per_position:
        for pos in config.get('per_position_filters', {}).get('positions', []):
            print(f"\n── get_dynasty_rankings(position={pos}) ──")
            try:
                payload = client.get_dynasty_rankings(position=pos)
            except Exception as e:
                failures[f'dynasty[{pos}]'] = [f"request failed: {type(e).__name__}: {e}"]
                continue
            spec = dict(EXPECTED['get_dynasty_rankings'])
            spec['filters_expected'] = {'position_id': pos, 'scoring': 'PPR',
                                        'ranking_type_name': 'dynasty'}
            ok, problems = validate_endpoint(f'dynasty[{pos}]', payload, spec)
            if ok:
                print('  OK')
            else:
                failures[f'dynasty[{pos}]'] = problems
                for p in problems:
                    print(f"  MISMATCH: {p}")

    print('\n' + '═' * 70)
    if failures:
        print(f"{len(failures)} endpoint(s) with mismatches:")
        for name, problems in failures.items():
            print(f"  {name}: {problems}")
        return 1
    print('All endpoints match code assumptions.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
