"""
prefetch.py - Bulk daily prefetch of FantasyPros datasets.

Modeled on `prefetch_all_team_data` in NCAAProjectCH `matchup_params.py`
(lines 158-227): one bulk call per dataset, everything cached, result indexed
for downstream use.

Budget math: the superflex/PPR/dynasty board needs 3 calls (dynasty rankings,
ADP, player universe) plus 4 optional per-position calls = 7 of the 50/day
budget for a full refresh. With the 24h TTL, re-running the same day costs 0.
"""

from typing import Any, Dict, List, Optional

from cache import SQLiteCache, cached_call
from config import load_config
from names import normalized_key

RANKINGS_ENDPOINT = 'consensus-rankings'


def _players(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        players = payload.get('players')
        if isinstance(players, list):
            return players
    return []


def _index(players: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index a rankings payload by player_id (as str) and by normalized name."""
    out: Dict[str, Dict[str, Any]] = {}
    for p in players:
        pid = p.get('player_id')
        if pid is not None:
            out[str(pid)] = p
        name = p.get('player_name')
        if name:
            out[normalized_key(name)] = p
    return out


def prefetch_all_player_data(client, cache: SQLiteCache, config: Optional[Dict[str, Any]] = None,
                             include_per_position: bool = True,
                             include_players: bool = True) -> Dict[str, Any]:
    """
    Fetch every dataset the board needs, in as few API calls as possible.

    All pulls use the league filters from config.yaml (sport=NFL, scoring=PPR,
    position=OP superflex, type=dynasty, current season). Cache keys include
    those filters so formats never collide.

    Returns:
        dict with:
          'filters': the filters used
          'dynasty': {'raw', 'players', 'by_id_or_name'}  (superflex board baseline)
          'adp':     {'raw', 'players', 'by_id_or_name'}
          'players': {'raw', 'players', 'by_id_or_name'}  (id/metadata universe)
          'per_position': {POS: {'raw', 'players', 'by_id_or_name'}}
          'calls_attempted': number of datasets fetched this run
    """
    config = config or load_config()
    filters = dict(config.get('api_filters', {}))
    ttl = config.get('cache', {}).get('default_ttl_seconds', 86400)
    season = filters.get('season')

    def bundle(payload):
        players = _players(payload)
        return {'raw': payload, 'players': players, 'by_id_or_name': _index(players)}

    def fetch(dataset: str, params: Dict[str, Any], fetch_fn):
        return cached_call(cache, f"{RANKINGS_ENDPOINT}/{dataset}", params, fetch_fn,
                           ttl=ttl, config=config)

    base_params = {
        'sport': filters.get('sport', 'NFL'),
        'season': season,
        'scoring': filters.get('scoring', 'PPR'),
        'position': filters.get('position', 'OP'),
        'week': filters.get('week', 0),
    }

    result: Dict[str, Any] = {'filters': filters, 'per_position': {}, 'calls_attempted': 0}

    dynasty_params = {**base_params, 'type': 'dynasty'}
    result['dynasty'] = bundle(fetch('dynasty', dynasty_params, lambda: client.get_dynasty_rankings()))
    result['calls_attempted'] += 1

    # ADP only exists for position=ALL (informational only — the board baseline
    # stays on the superflex OP dynasty rankings).
    adp_params = {**base_params, 'type': 'adp',
                  'position': filters.get('adp_position', 'ALL')}
    result['adp'] = bundle(fetch('adp', adp_params, lambda: client.get_adp()))
    result['calls_attempted'] += 1

    if include_players:
        players_params = {'sport': filters.get('sport', 'NFL'), 'season': season, 'position': 'ALL'}
        result['players'] = bundle(fetch('universe', players_params, lambda: client.get_players()))
        result['calls_attempted'] += 1
    else:
        result['players'] = bundle(None)

    if include_per_position:
        # Per-position pulls are for depth/sanity checks only — the overall
        # board baseline stays on the superflex (OP) rankings.
        for pos in config.get('per_position_filters', {}).get('positions', []):
            params = {**base_params, 'position': pos, 'type': 'dynasty'}
            payload = fetch(f'dynasty_{pos}', params,
                            lambda pos=pos: client.get_dynasty_rankings(position=pos))
            result['per_position'][pos] = bundle(payload)
            result['calls_attempted'] += 1

    return result


def rankings_to_rows(prefetched: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Flatten the prefetched superflex/dynasty rankings into board rows.

    FP_Baseline is a value score derived from the superflex ECR rank
    (higher = more valuable), so it is directly averageable with other sites'
    dollar/value baselines after scaling in the value model.
    """
    dynasty_players = prefetched.get('dynasty', {}).get('players', [])
    per_position = prefetched.get('per_position', {})
    universe = prefetched.get('players', {}).get('by_id_or_name', {})
    adp_index = prefetched.get('adp', {}).get('by_id_or_name', {})

    seen: Dict[str, Dict[str, Any]] = {}

    def add(p: Dict[str, Any], source: str):
        name = p.get('player_name')
        if not name:
            return
        key = normalized_key(name)
        meta = universe.get(str(p.get('player_id')), {}) or universe.get(key, {})
        adp_row = adp_index.get(str(p.get('player_id'))) or adp_index.get(key) or {}
        row = {
            'Player': name,
            'Position': p.get('player_position_id') or meta.get('position_id') or '',
            'Team': p.get('player_team_id') or meta.get('team_id') or '',
            'Bye': p.get('player_bye_week') or meta.get('bye_week') or '',
            'FP_PlayerId': p.get('player_id'),
            'FP_RankEcr': p.get('rank_ecr'),
            'FP_RankAve': p.get('rank_ave'),
            'FP_PosRank': p.get('pos_rank'),
            'FP_Tier': p.get('tier'),
            'FP_Adp': adp_row.get('rank_ecr') or adp_row.get('rank_ave'),
            'FP_Source': source,
        }
        existing = seen.get(key)
        if existing is None or (existing['FP_Source'] != 'superflex' and source == 'superflex'):
            seen[key] = row

    for p in dynasty_players:
        add(p, 'superflex')
    for pos, bundle in per_position.items():
        for p in bundle.get('players', []):
            add(p, f'position:{pos}')

    return list(seen.values())
