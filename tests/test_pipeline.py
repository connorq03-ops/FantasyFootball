"""Focused tests for the rate limiter, payload guards and pot reconciliation."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build_board import apply_sold, compute_team_budgets  # noqa: E402
from cache import SQLiteCache  # noqa: E402
from prefetch import IncompletePayload, rankings_to_rows, validate_payload  # noqa: E402
from value_model import reconcile_to_pot  # noqa: E402

CONFIG = {'value_model': {'reconcile': {'min_value': 1, 'enabled': True}}}


def _cache(tmp_path):
    return SQLiteCache(db_path=str(tmp_path / 'c.db'), default_ttl=86400)


def test_rate_limit_is_rolling_not_fixed_window(tmp_path):
    cache = _cache(tmp_path)
    assert cache.check_rate_limit('fp', max_calls=2, window_seconds=86400)
    assert cache.check_rate_limit('fp', max_calls=2, window_seconds=86400)
    assert not cache.check_rate_limit('fp', max_calls=2, window_seconds=86400)

    # Age only the first call past the window: a fixed window would reset the
    # counter to zero here and let two more calls through.
    conn = cache._conn()
    oldest = conn.execute('SELECT id, ts FROM api_calls ORDER BY ts LIMIT 1').fetchone()
    conn.execute('UPDATE api_calls SET ts = ? WHERE id = ?', (oldest[1] - 86401, oldest[0]))

    assert cache.check_rate_limit('fp', max_calls=2, window_seconds=86400)
    assert not cache.check_rate_limit('fp', max_calls=2, window_seconds=86400)


def test_cache_hit_does_not_consume_budget(tmp_path):
    cache = _cache(tmp_path)
    params = {'position': 'OP', 'type': 'dynasty'}
    cache.set('rankings', params, {'players': [{'player_name': 'A'}]})
    assert cache.get('rankings', params) is not None
    assert cache.stats()['rate_limits'] == {}


def test_free_tier_payload_rejected():
    payload = {'tier': 'free', 'limit': 10, 'count': 437,
               'players': [{'player_name': f'P{i}'} for i in range(10)]}
    with pytest.raises(IncompletePayload):
        validate_payload(payload, 'dynasty')


def test_truncated_count_rejected():
    payload = {'tier': 'premium', 'count': 437, 'players': [{'player_name': 'A'}]}
    with pytest.raises(IncompletePayload):
        validate_payload(payload, 'dynasty')


def test_empty_dynasty_rejected_only_when_required():
    payload = {'tier': 'premium', 'count': 0, 'players': []}
    validate_payload(payload, 'adp')
    with pytest.raises(IncompletePayload):
        validate_payload(payload, 'dynasty', require_players=True)


def test_position_only_players_stay_off_the_board():
    prefetched = {
        'dynasty': {'players': [{'player_name': 'Overall Guy', 'rank_ecr': 1}]},
        'per_position': {'QB': {'players': [{'player_name': 'Deep QB', 'rank_ecr': 40}]}},
        'players': {'by_id_or_name': {}},
        'adp': {'by_id_or_name': {}},
    }
    assert [r['Player'] for r in rankings_to_rows(prefetched)] == ['Overall Guy']


def test_reconcile_hits_pot_exactly():
    df = pd.DataFrame({
        'RawAdj': [40.0, 20.0, 10.0, 1.0],
        'IsAvailable': [1, 1, 1, 1],
        'MarketScalar': [1.5] * 4,
    })
    out = reconcile_to_pot(df, 100, CONFIG)
    assert out['FinalAdj'].sum() == 100


def test_reconcile_when_pot_cannot_cover_dollar_floor():
    df = pd.DataFrame({
        'RawAdj': [5.0, 4.0, 3.0, 2.0, 1.0],
        'IsAvailable': [1] * 5,
        'MarketScalar': [0.1] * 5,
    })
    out = reconcile_to_pot(df, 3, CONFIG)
    assert out['FinalAdj'].sum() == 3
    # The cheapest tail is unrosterable with the money left.
    assert list(out['FinalAdj']) == [1, 1, 1, 0, 0]


def _keepers():
    return pd.DataFrame({
        'Team': ['AL', 'AL', 'RV'],
        'Manager': ['Andrew Latzke', 'Andrew Latzke', 'Blake Doerring'],
        'Player': ['A', 'B', 'C'],
        'KeeperCost': [10, 10, 20],
    })


def test_manager_spelling_difference_does_not_duplicate_budget(tmp_path):
    seed = tmp_path / 'team_budgets.csv'
    pd.DataFrame({
        'Team': ['AL', 'RV'],
        'Manager': ['Andrew  Latzke', 'Blake Doerring'],
        'StartingBudget': [200, 200],
    }).to_csv(seed, index=False)

    budgets = compute_team_budgets(_keepers(), str(seed), 200)
    assert len(budgets) == 2
    assert budgets['StartingBudget'].sum() == 400
    assert budgets.set_index('Team').at['AL', 'AvailableBudget'] == 180


def test_unknown_sold_winner_is_rejected():
    df = pd.DataFrame({'Player': ['A'], 'NameKey': ['a'], 'IsAvailable': [1], 'Tag': ['']})
    budgets = pd.DataFrame({'Team': ['AL'], 'Manager': ['Andrew Latzke'],
                            'StartingBudget': [200], 'KeeperSpend': [20],
                            'AvailableBudget': [180]})
    sold = pd.DataFrame({'Player': ['A'], 'SoldPrice': [10], 'WinningTeam': ['NOPE'],
                         'NameKey': ['a']})
    with pytest.raises(ValueError):
        apply_sold(df, sold, budgets, 1375)
