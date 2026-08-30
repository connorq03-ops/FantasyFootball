"""Focused tests for the rate limiter, payload guards and pot reconciliation."""

import math
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auction_values import compute_auction_values, projection_rows  # noqa: E402
from build_board import (  # noqa: E402
    add_contenders,
    apply_sold,
    build_competition_report,
    compute_team_budgets,
    fp_auction_baselines,
    load_keepers,
)
from draftsharks import parse_auction_values  # noqa: E402
from espn_cheatsheet import parse_cheatsheet  # noqa: E402
from fp_auction import form_payload, parse_values  # noqa: E402
from cache import CacheMiss, SQLiteCache, cached_call  # noqa: E402
from prefetch import (  # noqa: E402
    IncompletePayload,
    prefetch_all_player_data,
    rankings_to_rows,
    validate_payload,
)
from value_model import (  # noqa: E402
    assign_tier,
    apply_positional_scarcity,
    position_sanity_check,
    positional_scarcity_report,
    reconcile_to_pot,
    run_value_model,
    scarcity_premium,
    solve_for_pot,
)

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


def test_premium_curve_is_continuous_and_monotonic():
    config = {'value_model': {'premium': {
        'peak': 1.05, 'decay': 9.0, 'tail_factor': 0.95, 'round_digits': 3,
    }}}
    values = [scarcity_premium(rank, 'WR', config) for rank in range(1, 121)]
    assert all(left >= right for left, right in zip(values, values[1:]))
    assert abs(values[35] - values[36]) < 0.02


def test_surplus_solve_and_market_columns():
    raw = [10.0, 5.0]
    scalar = solve_for_pot(raw, 20, min_bid=1)
    assert scalar == pytest.approx((20 - 2) / ((10 - 1) + (5 - 1)))
    assert 1 + scalar * (raw[0] - 1) == pytest.approx(1 + scalar * 9)

    df = pd.DataFrame({
        'Player': [f'P{i}' for i in range(10)],
        'Position': ['QB', 'QB', 'RB', 'RB', 'WR', 'WR', 'WR', 'TE', 'TE', 'WR'],
        'FP_Baseline': [100 - i * 5 for i in range(10)],
        'DS_MarketValue': [100 - i * 5 for i in range(10)],
        'ESPN_Baseline': [100 - i * 5 for i in range(10)],
        'IsAvailable': [1] * 10,
        'Tag': [''] * 10,
    })
    config = {
        'league': {'teams': 2, 'draft_pool': {'enabled': True, 'roster_size': 5}},
        'value_model': {
            'baseline_columns': ['FP_Baseline', 'DS_MarketValue', 'ESPN_Baseline'],
            'reconcile': {'min_value': 1, 'enabled': True},
            'market': {'enabled': True, 'spend_rate': 0.92,
                       'position_bias': {'QB': 0.85, 'RB': 1.21, 'WR': 1.02, 'TE': 1.12}},
        },
    }
    out = run_value_model(df, 100, config)
    priced = out[out['InDraftPool'] == 1]
    assert priced['FinalAdj'].sum() == 100
    assert priced['MarketPrice'].sum() == round(100 * 0.92)
    assert (out['Edge'] == out['FinalAdj'] - out['MarketPrice']).all()


def test_qb_market_bias_only_changes_market_share():
    df = pd.DataFrame({
        'Player': [f'P{i}' for i in range(10)],
        'Position': ['QB', 'QB', 'QB', 'RB', 'RB', 'WR', 'WR', 'WR', 'TE', 'TE'],
        'FP_Baseline': [100 - i * 5 for i in range(10)],
        'IsAvailable': [1] * 10,
        'Tag': [''] * 10,
    })
    base = {
        'league': {'teams': 2, 'draft_pool': {'enabled': True, 'roster_size': 5}},
        'value_model': {
            'baseline_columns': ['FP_Baseline'],
            'reconcile': {'min_value': 1, 'enabled': True},
            'market': {'enabled': True, 'spend_rate': 0.92,
                       'position_bias': {'QB': 1.0, 'RB': 1.0, 'WR': 1.0, 'TE': 1.0}},
        },
    }
    biased = {**base, 'value_model': {**base['value_model'], 'market': {
        **base['value_model']['market'], 'position_bias': {
            'QB': 0.85, 'RB': 1.0, 'WR': 1.0, 'TE': 1.0}}}}
    neutral = run_value_model(df, 100, base)
    reduced = run_value_model(df, 100, biased)
    neutral_qb_final = neutral.loc[neutral['Position'] == 'QB', 'FinalAdj'].sum()
    reduced_qb_final = reduced.loc[reduced['Position'] == 'QB', 'FinalAdj'].sum()
    neutral_qb_market = neutral.loc[neutral['Position'] == 'QB', 'MarketPrice'].sum()
    reduced_qb_market = reduced.loc[reduced['Position'] == 'QB', 'MarketPrice'].sum()
    assert reduced_qb_final == neutral_qb_final
    assert reduced_qb_market / reduced['MarketPrice'].sum() < neutral_qb_market / neutral['MarketPrice'].sum()


def test_board_floor_uses_reconcile_min_value():
    df = pd.DataFrame({
        'Player': [f'P{i}' for i in range(4)],
        'Position': ['QB', 'RB', 'WR', 'TE'],
        'FP_Baseline': [40.0, 30.0, 20.0, 10.0],
        'IsAvailable': [1] * 4,
        'Tag': [''] * 4,
    })
    config = {
        'baseline_auction': {'min_bid': 5},
        'league': {'teams': 1, 'draft_pool': {'enabled': True, 'roster_size': 4}},
        'value_model': {
            'baseline_columns': ['FP_Baseline'],
            'reconcile': {'min_value': 2, 'enabled': True},
            'market': {'enabled': False},
        },
    }
    out = run_value_model(df, 100, config)
    priced = out[out['InDraftPool'] == 1]
    assert (priced['FinalAdj'] >= 2).all()
    assert priced['FinalAdj'].sum() == 100


def test_market_price_preserves_floor_when_spend_rate_would_break_it():
    df = pd.DataFrame({
        'Player': ['A', 'B', 'C'],
        'Position': ['QB', 'RB', 'WR'],
        'FP_Baseline': [30.0, 20.0, 10.0],
        'IsAvailable': [1] * 3,
        'Tag': [''] * 3,
    })
    config = {
        'league': {'teams': 1, 'draft_pool': {'enabled': True, 'roster_size': 3}},
        'value_model': {
            'baseline_columns': ['FP_Baseline'],
            'reconcile': {'min_value': 2, 'enabled': True},
            'market': {'enabled': True, 'spend_rate': 0.92,
                       'position_bias': {'QB': 1.0, 'RB': 1.0, 'WR': 1.0}},
        },
    }
    out = run_value_model(df, 6, config)
    priced = out[out['InDraftPool'] == 1]
    assert (priced['MarketPrice'] >= 2).all()
    assert priced['MarketPrice'].sum() == 3 * 2


def _scarcity_frame():
    return pd.DataFrame({
        'Player': ['QB1', 'RB1', 'WR1', 'WR2', 'WR3', 'WR4', 'TE1',
                   'RB_keeper', 'TE_keeper'],
        'Position': ['QB', 'RB', 'WR', 'WR', 'WR', 'WR', 'TE', 'RB', 'TE'],
        'FP_Baseline': [20.0] * 9,
        'IsAvailable': [1] * 7 + [0, 0],
        'Tag': [''] * 7 + ['Keeper', 'Keeper'],
    })


def _scarcity_config(enabled=True):
    return {
        'league': {
            'teams': 1,
            'draft_pool': {'enabled': True, 'roster_size': 9},
            'roster_slots': {'QB': 1, 'RB': 2, 'WR': 3, 'TE': 1, 'FLEX': 1},
        },
        'value_model': {
            'baseline_columns': ['FP_Baseline'],
            'scarcity': {
                'enabled': enabled, 'alpha': 1.0,
                'min_factor': 0.75, 'max_factor': 1.5,
                'flex_positions': ['RB', 'WR', 'TE'],
            },
            'market': {
                'enabled': True, 'spend_rate': 0.92,
                'position_bias': {'QB': 1.0, 'RB': 1.0, 'WR': 1.0, 'TE': 1.0},
            },
            'reconcile': {'min_value': 1, 'enabled': True},
        },
    }


def test_positional_scarcity_uses_flex_and_clips_factors():
    df = _scarcity_frame()
    prepared = run_value_model(df, 100, _scarcity_config())
    report = positional_scarcity_report(prepared, _scarcity_config()).set_index('Position')
    assert report.at['RB', 'StarterSlots'] == pytest.approx(2 + 2 / 6, abs=0.001)
    assert report.at['WR', 'StarterSlots'] == pytest.approx(3 + 3 / 6, abs=0.001)
    assert report.at['TE', 'StarterSlots'] == pytest.approx(1 + 1 / 6, abs=0.001)
    assert report.at['RB', 'Ratio'] == pytest.approx((2 + 2 / 6 - 1), abs=0.001)
    assert report.at['RB', 'PosScarcityFactor'] == 1.5
    assert report.at['TE', 'PosScarcityFactor'] == 0.75


def test_positional_scarcity_can_be_disabled():
    out = apply_positional_scarcity(_scarcity_frame(), _scarcity_config(False))
    assert set(out['PosScarcityFactor']) == {1.0}


def test_positional_scarcity_redistributes_final_adj_but_not_the_pot():
    df = _scarcity_frame()
    enabled = run_value_model(df, 100, _scarcity_config(True))
    disabled = run_value_model(df, 100, _scarcity_config(False))
    assert enabled['FinalAdj'].sum() == disabled['FinalAdj'].sum() == 100
    enabled_shares = enabled.groupby('Position')['FinalAdj'].sum() / 100
    disabled_shares = disabled.groupby('Position')['FinalAdj'].sum() / 100
    assert any(enabled_shares[pos] != disabled_shares[pos]
               for pos in ('QB', 'RB', 'WR', 'TE'))


def test_positional_scarcity_does_not_leak_into_market_price():
    df = _scarcity_frame()
    enabled = run_value_model(df, 100, _scarcity_config(True))
    disabled = run_value_model(df, 100, _scarcity_config(False))
    pd.testing.assert_series_equal(
        enabled['MarketPrice'].reset_index(drop=True),
        disabled['MarketPrice'].reset_index(drop=True),
        check_names=False,
    )
    assert enabled['MarketPrice'].sum() == disabled['MarketPrice'].sum()
    assert any(
        enabled.loc[enabled['Position'] == pos, 'FinalAdj'].sum()
        != disabled.loc[disabled['Position'] == pos, 'FinalAdj'].sum()
        for pos in ('QB', 'RB', 'WR', 'TE')
    )


def test_positional_scarcity_zero_supply_is_safe():
    df = pd.DataFrame({
        'Player': ['QB', 'TE_keeper'],
        'Position': ['QB', 'TE'],
        'FP_Baseline': [20.0, 10.0],
        'IsAvailable': [1, 0],
        'Tag': ['', 'Keeper'],
    })
    config = _scarcity_config()
    out = run_value_model(df, 20, config)
    report = positional_scarcity_report(out, config).set_index('Position')
    assert report.at['TE', 'Supply'] == 0
    assert report.at['TE', 'PosScarcityFactor'] == 1.0
    assert out['FinalAdj'].sum() == 20


def test_competition_counts_rivals_and_zeroes_unpriced_rows():
    budgets = pd.DataFrame({
        'Manager': ['Connor Haley', 'Blake Doerring', 'Andrew Latzke'],
        'Team': ['CON', 'RV', 'AL'],
        'AvailableBudget': [132, 80, 180],
    })
    keepers = pd.DataFrame({
        'Manager': ['Connor Haley', 'Blake Doerring', 'Andrew Latzke'],
        'Team': ['CON', 'RV', 'AL'],
        'Player': ['C', 'B', 'A'],
    })
    config = {
        'league': {'my_manager': 'Connor Haley',
                   'draft_pool': {'roster_size': 15}},
        'value_model': {'reconcile': {'min_value': 1}},
    }
    competition = build_competition_report(budgets, keepers, config)
    assert competition['MaxBid'].tolist() == [167, 119, 67]
    board = pd.DataFrame({
        'IsAvailable': [1, 1, 1, 1],
        'InDraftPool': [1, 1, 0, 0],
        'FinalAdj': [100, 60, 60, 0],
    })
    out = add_contenders(board, competition, config)
    assert out['Contenders'].tolist() == [1, 2, 0, 0]


def _projections(counts=(('QB', 30), ('RB', 60), ('WR', 80), ('TE', 30))):
    payload = {'players': []}
    for pos, count in counts:
        for i in range(count):
            payload['players'].append({
                'name': f'{pos}{i}', 'position_id': pos, 'team_id': 'X',
                'fpid': f'{pos}{i}', 'stats': {'points_ppr': 400.0 - i * 5.0},
            })
    return payload


BASELINE_CONFIG = {'baseline_auction': {'teams': 10, 'budget': 200, 'roster_size': 15}}


def test_auction_values_are_absolute_dollars_summing_to_the_reference_pot():
    values = compute_auction_values(projection_rows(_projections()), BASELINE_CONFIG)
    priced = [v for v in values if v['AuctionValue'] > 0]
    assert len(priced) == 150                       # teams x roster_size
    assert round(sum(v['AuctionValue'] for v in priced)) == 2000
    # Everyone outside the rosterable pool is published at $0, not a floor bid.
    assert all(v['AuctionValue'] == 0 for v in values[150:])
    assert values[0]['AuctionValue'] > values[100]['AuctionValue'] > 1


def test_baseline_ignores_league_state():
    """Keepers, pot and draft state are league state; the source value is not."""
    rows = projection_rows(_projections())
    before = compute_auction_values(rows, BASELINE_CONFIG)
    after = compute_auction_values(rows, {**BASELINE_CONFIG, 'league': {
        'teams': 4, 'keepers_per_team': 8, 'starting_budget': 50,
        'draft_pool': {'roster_size': 3}}})
    assert [v['AuctionValue'] for v in before] == [v['AuctionValue'] for v in after]


def test_value_model_never_rewrites_the_source_baseline():
    df = pd.DataFrame({
        'Player': [f'P{i}' for i in range(12)],
        'Position': ['WR'] * 12,
        'FP_Baseline': [float(60 - i * 4) for i in range(12)],
        'DS_MarketValue': [float(58 - i * 3) for i in range(12)],
        'ESPN_Baseline': [float(56 - i * 2) for i in range(12)],
        'IsAvailable': [1] * 10 + [0, 0],
        'Tag': [''] * 10 + ['Keeper', 'Keeper'],
    })
    config = {
        'league': {'teams': 2, 'draft_pool': {'enabled': True, 'roster_size': 5}},
        'value_model': {'baseline_columns': ['FP_Baseline', 'DS_MarketValue', 'ESPN_Baseline'],
                        'reconcile': {'min_value': 1, 'enabled': True}},
    }
    out = run_value_model(df.copy(), 300, config)
    assert list(out['FP_Baseline']) == list(df['FP_Baseline'])
    assert list(out['DS_MarketValue']) == list(df['DS_MarketValue'])
    assert list(out['ESPN_Baseline']) == list(df['ESPN_Baseline'])
    # The league adjustment lives in FinalAdj, which is free to differ.
    assert out.loc[0, 'FinalAdj'] != out.loc[0, 'FP_Baseline']


def test_pot_is_spread_over_rosterable_players_only():
    df = pd.DataFrame({
        'Player': [f'P{i}' for i in range(20)],
        'Position': ['WR'] * 20,
        'FP_Baseline': [float(20 - i) for i in range(20)],
        'IsAvailable': [1] * 20,
        'Tag': [''] * 20,
    })
    config = {
        'league': {'teams': 2, 'draft_pool': {'enabled': True, 'roster_size': 5}},
        'value_model': {'baseline_columns': ['FP_Baseline'],
                        'reconcile': {'min_value': 1, 'enabled': True}},
    }
    out = run_value_model(df, 100, config)
    priced = out[out['FinalAdj'] > 0]
    assert len(priced) == 10
    assert priced['FinalAdj'].sum() == 100
    assert set(out.loc[out['InDraftPool'] == 0, 'Tier']) == {'Undrafted'}


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

        def get_projections(self, **_kw):
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


def test_fractional_starting_budget_is_rejected(tmp_path):
    seed = tmp_path / 'team_budgets.csv'
    pd.DataFrame({'Team': ['AL', 'RV'], 'Manager': ['Andrew Latzke', 'Blake Doerring'],
                  'StartingBudget': [200.5, 200]}).to_csv(seed, index=False)
    with pytest.raises(ValueError, match='StartingBudget'):
        compute_team_budgets(_keepers(), str(seed), 200)


def test_blank_starting_budget_keeps_the_default(tmp_path):
    seed = tmp_path / 'team_budgets.csv'
    pd.DataFrame({'Team': ['AL', 'RV'], 'Manager': ['Andrew Latzke', 'Blake Doerring'],
                  'StartingBudget': ['', 200]}).to_csv(seed, index=False)
    budgets = compute_team_budgets(_keepers(), str(seed), 200)
    assert budgets.set_index('Team').at['AL', 'StartingBudget'] == 200


def test_keeper_overspend_is_rejected():
    keepers = pd.DataFrame({'Team': ['AL'], 'Manager': ['Andrew Latzke'],
                            'Player': ['A'], 'KeeperCost': [250]})
    with pytest.raises(ValueError, match='overspends'):
        compute_team_budgets(keepers, None, 200)


def test_cache_only_miss_reserves_no_budget(tmp_path):
    cache = _cache(tmp_path)
    config = {'rate_limit': {'api_name': 'fp', 'max_calls': 5, 'window_seconds': 86400}}

    def _never():
        raise AssertionError('fetch_fn must not run in cache-only mode')

    with pytest.raises(CacheMiss):
        cached_call(cache, 'rankings/dynasty', {'position': 'OP'}, _never, config=config,
                    cache_only=True)
    assert cache.stats(max_calls=5)['rate_limits'] == {}


DRAFT_WIZARD_HTML = """
<table class='ValueTable' id='OverallTable'><thead><th>#</th></thead><tbody>
<tr pid='17298' v='47' pts='361' class=' PlayerQB''><td class='RankCell'></td>
<td>Josh Allen (BUF - QB)</td><td>361</td><td>$47</td></tr>
<tr pid='22968' v='39' pts='373' class=' PlayerRB''><td class='RankCell'></td>
<td>Jahmyr Gibbs (DET - RB)</td><td>373</td><td>$39</td></tr>
<tr pid='18219' v='6' pts='195' class=' PlayerWR''><td class='RankCell'></td>
<td>DK Metcalf (PIT - WR)<span class='injury-tag' title="Knee">DTD</span></td>
<td>195</td><td>$6</td></tr>
</tbody></table>
<table class='ValueTable' id='QBTable'><tbody>
<tr pid='17298' v='47' pts='361'><td class='RankCell'></td><td>Josh Allen, BUF</td></tr>
</tbody></table>
"""


def test_draft_wizard_rows_parse_into_source_dollars():
    rows = parse_values(DRAFT_WIZARD_HTML)
    assert [(r['Player'], r['Position'], r['FP_Baseline']) for r in rows] == [
        ('Josh Allen', 'QB', 47.0),
        ('Jahmyr Gibbs', 'RB', 39.0),
        ('DK Metcalf', 'WR', 6.0),
    ]
    assert rows[0]['FP_Points'] == 361.0 and rows[0]['FP_PlayerId'] == 17298


def test_reference_format_drives_the_draft_wizard_request():
    payload = form_payload({'baseline_auction': {
        'teams': 10, 'budget': 200, 'roster_size': 15,
        'slots': {'QB': 1, 'RB': 2, 'WR': 3, 'TE': 1, 'DST': 0, 'K': 0, 'QB/WR/RB/TE': 1},
    }})
    assert payload['teams'] == '10' and payload['tb'] == '200'
    assert payload['QB/WR/RB/TE'] == '1'      # superflex slot
    assert payload['BN'] == '7'               # 15 roster spots - 8 starters
    assert payload['recWR'] == '1'            # full PPR
    assert payload['showAuction'] == 'on'


def test_published_values_win_over_the_local_reconstruction():
    published = pd.DataFrame({'Player': ['Josh Allen'], 'FP_Baseline': [47.0],
                              'FP_Points': [361.0], 'NameKey': ['joshallen']})
    prefetched = {'projections': {'raw': _projections()}}
    values = fp_auction_baselines(prefetched, BASELINE_CONFIG, published)
    assert values == {'joshallen': {'AuctionValue': 47.0, 'Points': 361.0}}
    fallback = fp_auction_baselines(prefetched, BASELINE_CONFIG, None)
    assert 'joshallen' not in fallback and fallback


DRAFT_SHARKS_HTML = """
<tbody data-player-row data-player-name="Ja'Marr Chase" data-fantasy-position="WR">
<td><span data-value="$47" data-attribute="auctionMarketValue">$47</span></td>
<td><span data-value="92.7" data-attribute="dsValue">92.7</span></td>
</tbody>
<tbody data-player-row data-player-name="Pat Freiermuth" data-fantasy-position="TE">
<td><span data-value="$1" data-attribute="auctionMarketValue">$1</span></td>
<td><span data-value="-11.7" data-attribute="dsValue">-11.7</span></td>
</tbody>
"""


def test_draft_sharks_rows_parse_into_the_second_baseline():
    rows = parse_auction_values(DRAFT_SHARKS_HTML)
    assert rows == [
        {'Player': "Ja'Marr Chase", 'Position': 'WR',
         'DS_MarketValue': 47.0, 'DS_Value': 92.7},
        {'Player': 'Pat Freiermuth', 'Position': 'TE',
         'DS_MarketValue': 1.0, 'DS_Value': -11.7},
    ]


def test_both_baselines_average_without_either_being_rescaled():
    config = {'value_model': {'baseline_columns': ['FP_Baseline', 'DS_MarketValue'],
                              'premium': {'peak': 1.0, 'decay': 6.0},
                              'low_value': {'factor': 1.0, 'rank_cutoff': 999},
                              'reconcile': {'min_value': 1, 'enabled': True},
                              'tiers': [{'name': 'Tier 1', 'min_final_adj': 0}]}}
    df = pd.DataFrame({
        'Player': ['Chase', 'Lamb', 'Deep Guy'],
        'Position': ['WR', 'WR', 'WR'],
        'FP_Baseline': [38.0, 23.0, 0.0],
        'DS_MarketValue': [48.0, 41.0, math.nan],   # Draft Sharks doesn't price him
        'IsAvailable': [1, 1, 1],
    })
    out = run_value_model(df.copy(), 100, config)
    assert list(out['Avg_Baseline']) == [43.0, 32.0, 0.0]
    assert list(out['FP_Baseline']) == [38.0, 23.0, 0.0]
    assert list(out['DS_MarketValue'])[:2] == [48.0, 41.0]


ESPN_CHEATSHEET_TEXT = """2026 ESPN Fantasy Football Draft Kit
PPR Superflex Cheat Sheet
RANKINGS 1-80 RANKINGS 81-160
1. (QB1) Josh Allen, BUF $59 7 81. (QB20) Baker Mayfield, TB $4 10
9. (RB3) Christian McCaffrey, SF $49 8 85. (WR32) DK Metcalf, PIT $4 9
161. (RB49) Keaton Mitchell, LAC $0 7 169. (DST1) Texans D/ST, HOU $0 8
"""


def test_espn_cheatsheet_rows_parse_into_source_dollars():
    rows = parse_cheatsheet(ESPN_CHEATSHEET_TEXT)
    assert [row['ESPN_Rank'] for row in rows] == [1, 9, 81, 85, 161, 169]
    assert rows[0] == {'Player': 'Josh Allen', 'Position': 'QB', 'Team': 'BUF',
                       'ESPN_Baseline': 59.0, 'ESPN_Rank': 1, 'Bye': 7}
    # Players ESPN does not price are published as $0, not dropped.
    assert rows[4]['ESPN_Baseline'] == 0.0
    assert rows[3]['Player'] == 'DK Metcalf'


def test_espn_cheatsheet_ignores_the_repeated_header_banner():
    duplicated = '1. (QB1) Josh Allen, BUF $59 7\n' + ESPN_CHEATSHEET_TEXT
    assert len(parse_cheatsheet(duplicated)) == len(parse_cheatsheet(ESPN_CHEATSHEET_TEXT))
