"""
auction_values.py - Absolute auction dollars from FantasyPros projections.

FantasyPros' public API publishes consensus PROJECTIONS (points) and RANKS, not
auction dollars, so the source dollar value is derived the same way every
auction-value publisher derives it: value over replacement, converted to money
at a fixed reference format.

    replacement level = the first NON-starter at each position, after the flex
                        slots are handed to the best remaining RB/WR/TE
    VORP              = projected points - replacement points
    dollars           = min_bid + VORP share of (budget - min_bid per roster spot)

The reference format lives in `baseline_auction` in config.yaml (teams, budget,
starting lineup, roster size) and describes the FORMAT the dollars are quoted
in — superflex, PPR, $200 x 10. It is deliberately NOT read from `league`
state: keepers, sold players and the remaining pot must never move the source
baseline. League-specific effects are applied downstream in value_model.py.
"""

from typing import Any, Dict, Iterable, List, Optional

DEFAULT_REFERENCE = {
    'teams': 10,
    'budget': 200,
    'roster_size': 15,
    'starters': {'QB': 2, 'RB': 2, 'WR': 3, 'TE': 1, 'FLEX': 1},
    'flex_positions': ['RB', 'WR', 'TE'],
    'positions': ['QB', 'RB', 'WR', 'TE'],
    'min_bid': 1,
}


def reference_format(config: Dict[str, Any]) -> Dict[str, Any]:
    """The published-value reference format, defaults filled in."""
    cfg = dict(DEFAULT_REFERENCE)
    cfg.update(config.get('baseline_auction', {}) or {})
    return cfg


def projection_rows(payload: Any, scoring_field: str = 'points_ppr') -> List[Dict[str, Any]]:
    """Flatten a FantasyPros projections payload into {Player, Position, Points}."""
    players = payload.get('players', []) if isinstance(payload, dict) else []
    rows: List[Dict[str, Any]] = []
    for p in players:
        stats = p.get('stats') or {}
        points = stats.get(scoring_field, stats.get('points'))
        if points is None:
            continue
        rows.append({
            'Player': p.get('name', ''),
            'Position': p.get('position_id', ''),
            'Team': p.get('team_id', ''),
            'FP_PlayerId': p.get('fpid'),
            'Points': float(points),
        })
    return rows


def starter_counts(rows: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, int]:
    """
    League-wide starters per position, with flex slots allocated to whichever
    RB/WR/TE are actually the best remaining players.
    """
    teams = int(cfg['teams'])
    starters = cfg['starters']
    positions = list(cfg['positions'])
    counts = {pos: teams * int(starters.get(pos, 0)) for pos in positions}

    by_pos: Dict[str, List[float]] = {pos: [] for pos in positions}
    for row in rows:
        if row['Position'] in by_pos:
            by_pos[row['Position']].append(row['Points'])
    for pos in by_pos:
        by_pos[pos].sort(reverse=True)

    flex_slots = teams * int(starters.get('FLEX', 0))
    flex_positions = [p for p in cfg['flex_positions'] if p in by_pos]
    for _ in range(flex_slots):
        best_pos: Optional[str] = None
        best_points = float('-inf')
        for pos in flex_positions:
            idx = counts[pos]
            if idx < len(by_pos[pos]) and by_pos[pos][idx] > best_points:
                best_pos, best_points = pos, by_pos[pos][idx]
        if best_pos is None:
            break
        counts[best_pos] += 1
    return counts


def _level(pool: List[float], count: int) -> float:
    if not pool:
        return 0.0
    return pool[count] if count < len(pool) else pool[-1]


def replacement_points(rows: List[Dict[str, Any]], cfg: Dict[str, Any],
                       iterations: int = 10) -> Dict[str, float]:
    """
    Points scored by the first player at each position who does NOT get rostered.

    Starting slots alone are the wrong replacement level in a 2-QB league: teams
    also roster backup QBs, so ~30 of the ~32 startable QBs come off the board
    and the true fallback is a streamer, not QB21. The rostered count per
    position is therefore solved by iterating — rank everyone by VORP, see which
    `teams * roster_size` players are actually drafted, move each replacement
    level to that depth, repeat until the split stops moving.
    """
    by_pos: Dict[str, List[float]] = {}
    for row in rows:
        by_pos.setdefault(row['Position'], []).append(row['Points'])
    for pos in by_pos:
        by_pos[pos].sort(reverse=True)

    floors = starter_counts(rows, cfg)
    counts = dict(floors)
    spots = int(cfg['teams']) * int(cfg['roster_size'])

    for _ in range(iterations):
        levels = {pos: _level(pool, counts.get(pos, 0)) for pos, pool in by_pos.items()}
        ranked = sorted(rows, key=lambda r: r['Points'] - levels.get(r['Position'], 0.0),
                        reverse=True)[:spots]
        drafted: Dict[str, int] = {pos: 0 for pos in by_pos}
        for row in ranked:
            drafted[row['Position']] += 1
        updated = {pos: max(floors.get(pos, 0), drafted[pos]) for pos in by_pos}
        if updated == counts:
            break
        counts = updated

    return {pos: _level(pool, counts.get(pos, 0)) for pos, pool in by_pos.items()}


def compute_auction_values(rows: Iterable[Dict[str, Any]],
                           config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Absolute auction dollars for the reference format.

    Every roster spot in the reference league is funded: the `teams * budget`
    pot is handed out as `min_bid` per rosterable player plus a VORP-weighted
    share of what is left. Players outside the rosterable pool are published at
    $0 — they are not auction assets, and paying them a floor dollar is exactly
    what drains a real auction pot.
    """
    cfg = reference_format(config)
    positions = set(cfg['positions'])
    rows = [dict(r) for r in rows if r.get('Position') in positions]
    if not rows:
        return []

    levels = replacement_points(rows, cfg)
    for row in rows:
        row['Vorp'] = row['Points'] - levels.get(row['Position'], 0.0)

    teams = int(cfg['teams'])
    min_bid = float(cfg['min_bid'])
    spots = teams * int(cfg['roster_size'])
    pot = teams * float(cfg['budget'])

    ordered = sorted(rows, key=lambda r: r['Vorp'], reverse=True)
    pool = ordered[:spots]
    surplus = pot - min_bid * len(pool)
    positive = sum(r['Vorp'] for r in pool if r['Vorp'] > 0)

    for row in ordered:
        row['AuctionValue'] = 0.0
    for row in pool:
        share = (row['Vorp'] / positive) if (positive > 0 and row['Vorp'] > 0) else 0.0
        row['AuctionValue'] = min_bid + share * surplus
    return ordered
