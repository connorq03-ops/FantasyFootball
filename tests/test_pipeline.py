"""Focused tests for the rate limiter, payload guards and pot reconciliation."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build_board import apply_sold, compute_team_budgets, load_keepers  # noqa: E402
from cache import SQLiteCache  # noqa: E402
from prefetch import (  # noqa: E402
    IncompletePayload,
    prefetch_all_player_data,
    rankings_to_rows,
    validate_payload,
)
from value_model import assign_tier, position_sanity_check, reconcile_to_pot  # noqa: E402

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


def _board():
    return pd.DataFrame({
        'Player': ['A', 'B'], 'NameKey': ['a', 'b'],
        'IsAvailable': [1, 1], 'Tag': ['', ''],
    })


def _budgets(available=180):
    return pd.DataFrame({'Team': ['AL'], 'Manager': ['Andrew Latzke'],
                         'StartingBudget': [200], 'KeeperSpend': [20],
                         'AvailableBudget': [available]})


def test_bad_money_values_are_rejected(tmp_path):
    for raw in ('', 'abc', '10.5', '-5'):
        path = tmp_path / 'keepers.csv'
        pd.DataFrame({'Team': ['AL'], 'Manager': ['Andrew Latzke'], 'Player': ['A'],
                      'KeeperCost': [raw], 'KeeperYear': ['3/3']}).to_csv(path, index=False)
        with pytest.raises(ValueError):
            load_keepers(str(path))


def test_unknown_sold_player_is_rejected():
    sold = pd.DataFrame({'Player': ['Nobody At All'], 'SoldPrice': [10],
                         'WinningTeam': ['AL'], 'NameKey': ['nobodyatall']})
    with pytest.raises(ValueError, match='not on the board'):
        apply_sold(_board(), sold, _budgets(), 1375)


def test_duplicate_sale_is_rejected_before_any_debit():
    sold = pd.DataFrame({'Player': ['A', 'A'], 'SoldPrice': [10, 10],
                         'WinningTeam': ['AL', 'AL'], 'NameKey': ['a', 'a']})
    with pytest.raises(ValueError, match='twice'):
        apply_sold(_board(), sold, _budgets(), 1375)


def test_sale_over_team_budget_is_rejected():
    sold = pd.DataFrame({'Player': ['A'], 'SoldPrice': [200], 'WinningTeam': ['AL'],
                         'NameKey': ['a']})
    with pytest.raises(ValueError, match='overspends'):
        apply_sold(_board(), sold, _budgets(available=50), 1375)


def test_sale_over_league_pot_is_rejected():
    sold = pd.DataFrame({'Player': ['A'], 'SoldPrice': [100], 'WinningTeam': ['AL'],
                         'NameKey': ['a']})
    with pytest.raises(ValueError, match='league pot'):
        apply_sold(_board(), sold, _budgets(), 50)


def test_sold_players_are_not_labelled_as_keepers():
    df = pd.DataFrame({
        'Player': ['Keeper Guy', 'Sold Guy', 'Cheap Guy'],
        'Position': ['QB', 'RB', 'WR'],
        'Tag': ['Keeper', 'Sold $25 (AL)', ''],
        'IsAvailable': [0, 0, 1],
        'FinalAdj': [0, 0, 5],
    })
    config = {'value_model': {'tiers': [{'name': 'Value', 'min_final_adj': 0}]}}
    tiers = assign_tier(df, config)['Tier'].tolist()
    assert tiers == ['Keeper', 'Sold', 'Value']


def test_flex_is_in_the_slot_denominator():
    config = {'league': {'teams': 10,
                         'roster_slots': {'QB': 2, 'RB': 2, 'WR': 3, 'TE': 1, 'FLEX': 1}}}
    df = pd.DataFrame({'Position': ['QB'], 'FinalAdj': [500], 'IsAvailable': [1]})
    report = position_sanity_check(df, config, 1000)
    assert abs(float(report['SlotShare'].sum()) - 1.0) < 1e-3
    # QB share is 2/9 with FLEX counted, not 2/8.
    qb = report.set_index('Position').at['QB', 'SlotShare']
    assert round(qb, 4) == round(2 / 9, 4)
    assert 'FLEX (spent as RB/WR/TE)' in report['Position'].tolist()


def test_stats_reports_the_enforced_limit(tmp_path):
    cache = _cache(tmp_path)
    assert cache.check_rate_limit('fp', max_calls=3, window_seconds=86400)
    stats = cache.stats(window_seconds=86400, max_calls=3)
    assert stats['rate_limits']['fp'] == {'calls': 1, 'remaining': 2,
                                         'resets_in': stats['rate_limits']['fp']['resets_in']}


def test_reduced_prefetch_cache_still_builds_a_board(tmp_path):
    """A --no-players/--no-per-position cache must not force live calls later."""
    cache = _cache(tmp_path)
    config = {
        'api_filters': {'sport': 'NFL', 'season': 2026, 'scoring': 'PPR',
                        'position': 'OP', 'week': 0, 'adp_position': 'ALL'},
        'cache': {'default_ttl_seconds': 86400},
        'per_position_filters': {'positions': ['QB']},
        'rate_limit': {'api_name': 'fp', 'max_calls': 5, 'window_seconds': 86400},
    }
    payload = {'tier': 'premium', 'count': 1,
               'players': [{'player_name': 'Overall Guy', 'rank_ecr': 1}]}

    class _OnlyDynasty:
        def get_dynasty_rankings(self, **_kw):
            return payload

        def get_adp(self, **_kw):
            return {'tier': 'premium', 'count': 0, 'players': []}

        def get_players(self, **_kw):
            raise AssertionError('universe should not be fetched')

    prefetch_all_player_data(_OnlyDynasty(), cache, config,
                             include_per_position=False, include_players=False)

    class _Blocked:
        def __getattr__(self, name):
            def _fail(*_a, **_kw):
                raise RuntimeError('live call blocked')
            return _fail

    data = prefetch_all_player_data(_Blocked(), cache, config, optional_ok=True)
    assert [r['Player'] for r in rankings_to_rows(data)] == ['Overall Guy']
