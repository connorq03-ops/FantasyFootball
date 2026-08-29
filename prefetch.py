"""
prefetch.py - Bulk daily prefetch of FantasyPros datasets.

Modeled on `prefetch_all_team_data` in NCAAProjectCH `matchup_params.py`
(lines 158-227): one bulk call per dataset, everything cached, result indexed
for downstream use.

Budget math: the superflex/PPR/dynasty board needs 3 calls (dynasty rankings,
ADP, player universe) plus 4 optional per-position calls = 7 of the daily
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


class IncompletePayload(RuntimeError):
    """Raised when the API returned a truncated (e.g. free-tier) response."""


def validate_payload(payload: Any, dataset: str, require_players: bool = False) -> Any:
    """
    Reject truncated responses before they are cached or priced.

    A free-tier key answers with `tier: free` and only the top 10 rows while
    still advertising the full `count`. Treating that as complete would spread
    the whole auction pot over ten players, so it is a hard error. (Note
    `public_api_limited` is true on premium responses too, so it is not a
    usable signal — `tier` and the count/length mismatch are.)
    """
    players = _players(payload)
    if isinstance(payload, dict):
        if str(payload.get('tier', '')).lower() == 'free':
            raise IncompletePayload(
                f"{dataset}: API returned a free-tier response "
                f"(limit={payload.get('limit')!r}), which truncates rankings. "
                f"A premium FANTASYPROS_API_KEY is required.")
        count = payload.get('count')
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = None
        if count is not None and count > len(players):
            raise IncompletePayload(
                f"{dataset}: API advertised {count} players but returned "
                f"{len(players)} - response is truncated.")
    if require_players and not players:
        raise IncompletePayload(f"{dataset}: API returned no players.")
    return payload


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
                             include_players: bool = True,
                             optional_ok: bool = False) -> Dict[str, Any]:
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

    def fetch(dataset: str, params: Dict[str, Any], fetch_fn, require_players: bool = False,
              optional: bool = False):
        # Validating inside fetch_fn keeps a truncated response out of the cache
        # (so it can never overwrite a complete entry); a cache hit is
        # re-validated on the way out.
        def guarded():
            return validate_payload(fetch_fn(), dataset, require_players)

        try:
            payload = cached_call(cache, f"{RANKINGS_ENDPOINT}/{dataset}", params, guarded,
                                  ttl=ttl, config=config)
        except Exception as exc:
            # With optional_ok (board builds), a dataset the last prefetch skipped
            # must not abort the run: only the dynasty pull is needed for pricing.
            if optional and optional_ok:
                print(f"  ! skipping optional dataset {dataset}: {exc}")
                return None
            raise
        return validate_payload(payload, dataset, require_players)

    base_params = {
        'sport': filters.get('sport', 'NFL'),
        'season': season,
        'scoring': filters.get('scoring', 'PPR'),
        'position': filters.get('position', 'OP'),
        'week': filters.get('week', 0),
    }

    result: Dict[str, Any] = {'filters': filters, 'per_position': {}, 'calls_attempted': 0}

    dynasty_params = {**base_params, 'type': 'dynasty'}
    result['dynasty'] = bundle(fetch('dynasty', dynasty_params,
                                     lambda: client.get_dynasty_rankings(),
                                     require_players=True))
    result['calls_attempted'] += 1

    # ADP only exists for position=ALL (informational only — the board baseline
    # stays on the superflex OP dynasty rankings).
    adp_params = {**base_params, 'type': 'adp',
                  'position': filters.get('adp_position', 'ALL')}
    result['adp'] = bundle(fetch('adp', adp_params, lambda: client.get_adp(), optional=True))
    result['calls_attempted'] += 1

    if include_players:
        players_params = {'sport': filters.get('sport', 'NFL'), 'season': season, 'position': 'ALL'}
        result['players'] = bundle(fetch('universe', players_params, lambda: client.get_players(),
                                         optional=True))
        result['calls_attempted'] += 1
    else:
        result['players'] = bundle(None)

    if include_per_position:
        # Per-position pulls are for depth/sanity checks only — the overall
        # board baseline stays on the superflex (OP) rankings.
        for pos in config.get('per_position_filters', {}).get('positions', []):
            params = {**base_params, 'position': pos, 'type': 'dynasty'}
            payload = fetch(f'dynasty_{pos}', params,
                            lambda pos=pos: client.get_dynasty_rankings(position=pos),
                            optional=True)
            if payload is None:
                continue
            result['per_position'][pos] = bundle(payload)
            result['calls_attempted'] += 1

    return result


def rankings_to_rows(prefetched: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Flatten the prefetched superflex/dynasty rankings into board rows.

    FP_Baseline is a value score derived from the superflex ECR rank
    (higher = more valuable), so it is directly averageable with other sites'
    dollar/value baselines after scaling in the value model.

    Only the superflex (`OP`) dynasty pull produces priced rows. Per-position
    ranks are NOT comparable to overall ranks (QB12 is not the 12th most
    valuable player), so adding position-only players would both mis-price them
    and dilute every other player's share of the pot; those datasets stay in
    `prefetched['per_position']` for depth/sanity reporting only.
    """
    dynasty_players = prefetched.get('dynasty', {}).get('players', [])
    universe = prefetched.get('players', {}).get('by_id_or_name', {})
    adp_index = prefetched.get('adp', {}).get('by_id_or_name', {})

    seen: Dict[str, Dict[str, Any]] = {}

    def add(p: Dict[str, Any]):
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
            'FP_Source': 'superflex',
        }
        seen.setdefault(key, row)

    for p in dynasty_players:
        add(p)

    return list(seen.values())
