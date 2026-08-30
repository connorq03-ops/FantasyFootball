"""
calibrate.py - Fit the value-model knobs against last season's actual prices.

`last_year_auction.csv` is the league's own auction result (160 picks, keepers
flagged), so the non-keeper rows are the closest thing to ground truth we have:
$1,362 spent over 110 players, which is nearly the same shape of problem as this
year's board ($1,375 over 120).

The score is the distance between our board and that result on things we can
actually observe:

    * the top-N concentration profile (top 5/10/20/30/60 share of the pot)
    * the quantile shape of the tail (p75 / median / p25)
    * the most expensive single player, as a share of the pot
    * spend by position, as a share of the pot

Usage:
    python calibrate.py                       # score the current config
    python calibrate.py --grid                # search premium peak/decay/tail
    python calibrate.py --positions           # fit position multipliers

Nothing here writes to config.yaml; it prints the fit so the knobs can be set
deliberately.
"""

import argparse
import copy
import itertools
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import yaml

from value_model import run_value_model

CUTS = (5, 10, 20, 30, 60)
QUANTILES = (75, 50, 25)


def load_actuals(path: str = 'last_year_auction.csv') -> pd.DataFrame:
    """Last season's non-keeper, non-K/DST sales: what the room actually paid."""
    df = pd.read_csv(path)
    return df[(df['IsKeeper'] == 0) & (~df['Position'].isin(['K', 'DST']))].copy()


def profile(prices: List[float], positions: List[str]) -> Dict[str, float]:
    """Shape of a set of auction prices, normalized so pots of any size compare."""
    ordered = np.sort(np.asarray(prices, dtype=float))[::-1]
    total = ordered.sum()
    out = {f'top{n}': float(ordered[:n].sum() / total) for n in CUTS}
    for q in QUANTILES:
        out[f'p{q}'] = float(np.percentile(ordered, q) / total)
    out['max'] = float(ordered[0] / total)
    frame = pd.DataFrame({'Price': prices, 'Position': positions})
    for pos in ('QB', 'RB', 'WR', 'TE'):
        out[f'share_{pos}'] = float(frame.loc[frame['Position'] == pos, 'Price'].sum() / total)
    return out


def score(candidate: Dict[str, float], target: Dict[str, float],
          position_weight: float = 1.0) -> float:
    """Weighted absolute distance between two profiles (lower is better)."""
    total = 0.0
    for key, value in target.items():
        weight = position_weight if key.startswith('share_') else 1.0
        if key.startswith('p'):
            weight *= 20.0        # quantiles are tiny numbers; put them on scale
        total += weight * abs(candidate[key] - value)
    return total


def board_profile(frame: pd.DataFrame, pot: float, config: Dict[str, Any]) -> Dict[str, float]:
    out = run_value_model(frame.copy(), pot, config)
    priced = out[(out['IsAvailable'] == 1) & (out['InDraftPool'] == 1)]
    return profile(priced['FinalAdj'].tolist(), priced['Position'].tolist())


def with_premium(config: Dict[str, Any], peak: float, decay: float,
                 tail: float, low_value: bool) -> Dict[str, Any]:
    cfg = copy.deepcopy(config)
    premium = cfg['value_model']['premium']
    premium.update({'peak': peak, 'decay': decay, 'tail_factor': tail})
    if not low_value:
        cfg['value_model']['low_value'] = {'factor': 1.0, 'rank_cutoff': None,
                                          'baseline_cutoff': None}
    return cfg


def grid(frame: pd.DataFrame, pot: float, config: Dict[str, Any],
         target: Dict[str, float]) -> List[Tuple[float, Dict[str, float]]]:
    results = []
    peaks = [1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.4]
    decays = [4.0, 6.0, 9.0, 12.0, 18.0]
    tails = [0.85, 0.9, 0.95, 1.0]
    for peak, decay, tail, low_value in itertools.product(peaks, decays, tails, (False, True)):
        cfg = with_premium(config, peak, decay, tail, low_value)
        got = board_profile(frame, pot, cfg)
        results.append((score(got, target), {'peak': peak, 'decay': decay,
                                             'tail_factor': tail,
                                             'low_value': low_value, **got}))
    results.sort(key=lambda row: row[0])
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', required=True, help='a built board CSV to re-score')
    parser.add_argument('--pot', type=float, default=1375.0)
    parser.add_argument('--grid', action='store_true')
    parser.add_argument('--top', type=int, default=10)
    args = parser.parse_args()

    config = yaml.safe_load(open('config.yaml'))
    actual = load_actuals()
    target = profile(actual['Price'].tolist(), actual['Position'].tolist())
    frame = pd.read_csv(args.board)

    current = board_profile(frame, args.pot, config)
    keys = list(target)
    print(f'{"metric":10} {"actual":>8} {"current":>8}')
    for key in keys:
        print(f'{key:10} {target[key]:8.3f} {current[key]:8.3f}')
    print(f'\ncurrent score: {score(current, target):.3f}  (0 = identical shape)')

    if args.grid:
        print('\nbest candidates:')
        header = f'{"score":>6} {"peak":>5} {"decay":>6} {"tail":>5} {"lowval":>7}  ' \
                 + ' '.join(f'{k:>7}' for k in keys)
        print(header)
        for value, row in grid(frame, args.pot, config, target)[:args.top]:
            print(f'{value:6.3f} {row["peak"]:5.2f} {row["decay"]:6.1f} '
                  f'{row["tail_factor"]:5.2f} {str(row["low_value"]):>7}  '
                  + ' '.join(f'{row[k]:7.3f}' for k in keys))


if __name__ == '__main__':
    main()
