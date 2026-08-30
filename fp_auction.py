"""
fp_auction.py - FantasyPros' OWN published auction values (Draft Wizard).

The public FantasyPros API (api.fantasypros.com/public/v2/json) exposes ranks
and projections but no auction dollars. The dollars FantasyPros publishes come
from the Draft Wizard auction calculator, which prices its consensus
projections for a specific league format:

    POST https://draftwizard.fantasypros.com/editor/createFromProjections.jsp
    -> <tr pid='17298' v='47' pts='361'>Josh Allen (BUF - QB)</tr>

`v` is the auction dollar value and `pts` the projected points behind it. Only
the rosterable pool is returned (teams x roster spots), which is exactly the
set of players that are auction assets.

The format posted here is `baseline_auction` in config.yaml (superflex, PPR,
$200 x 10) — the format the source dollars are QUOTED in. It is deliberately
not this season's league state: keepers, sold players and the remaining pot
must never move the source baseline, only RawAdj/FinalAdj downstream.
"""

import argparse
import csv
import re
from typing import Any, Dict, List, Optional

import requests

PAGE_URL = 'https://draftwizard.fantasypros.com/editor/createFromProjections.jsp'

# Full PPR. Draft Wizard takes raw scoring rules, not a scoring preset.
DEFAULT_SCORING = {
    'passTD': 4, 'passYdsPts': 1, 'passYdsUnit': 25, 'passInt': -2,
    'recTD': 6, 'recYdsPts': 1, 'recYdsUnit': 10,
    'recWR': 1, 'recRB': 1, 'recTE': 1,
    'rushTD': 6, 'rushYdsPts': 1, 'rushYdsUnit': 10,
    'fumble': -2, 'twoPtConv': 2,
}

# Superflex is expressed as a QB/WR/RB/TE slot on top of the single QB slot.
DEFAULT_SLOTS = {'QB': 1, 'RB': 2, 'WR': 3, 'TE': 1, 'DST': 0, 'K': 0,
                 'QB/WR/RB/TE': 1, 'WR/RB/TE': 0, 'WR/RB': 0, 'WR/TE': 0, 'RB/TE': 0}

ROW_RE = re.compile(
    r"<tr pid='(?P<pid>\d+)' v='(?P<value>-?\d+)' pts='(?P<points>-?\d+)'[^>]*>"
    r"\s*<td[^>]*></td>\s*<td>(?P<name>[^<(]+)\((?P<team>[A-Z]+) - (?P<pos>[A-Z]+)\)")

CSV_COLUMNS = ['Player', 'Position', 'Team', 'FP_PlayerId', 'FP_Baseline', 'FP_Points']


def form_payload(config: Dict[str, Any]) -> Dict[str, str]:
    """Draft Wizard form fields for the reference format in `baseline_auction`."""
    cfg = dict(config.get('baseline_auction', {}) or {})
    slots = dict(DEFAULT_SLOTS)
    slots.update(cfg.get('slots', {}) or {})
    scoring = dict(DEFAULT_SCORING)
    scoring.update(cfg.get('scoring', {}) or {})

    starters = sum(int(v) for v in slots.values())
    bench = max(0, int(cfg.get('roster_size', 15)) - starters)

    payload: Dict[str, Any] = {
        'sport': 'nfl', 'newFromProjections': 'Y', 'tab': 'tabO',
        'playerList': '', 'playerValues': '', 'playerPoints': '', 'scoringSystem': '',
        'showAuction': 'on',
        'teams': int(cfg.get('teams', 10)),
        'tb': int(cfg.get('budget', 200)),
        'BN': bench,
        'title': 'baseline',
    }
    payload.update(slots)
    payload.update(scoring)
    return {k: str(v) for k, v in payload.items()}


def parse_values(html: str) -> List[Dict[str, Any]]:
    """Pull {Player, Position, Team, FP_Baseline, FP_Points} out of the overall table."""
    start = html.find("id='OverallTable'")
    if start < 0:
        return []
    table = html[start:html.find('</table>', start)]
    rows: List[Dict[str, Any]] = []
    for m in ROW_RE.finditer(table):
        rows.append({
            'Player': m.group('name').strip(),
            'Position': m.group('pos'),
            'Team': m.group('team'),
            'FP_PlayerId': int(m.group('pid')),
            'FP_Baseline': float(m.group('value')),
            'FP_Points': float(m.group('points')),
        })
    return rows


def fetch_values(config: Dict[str, Any], url: str = PAGE_URL,
                 timeout: int = 90) -> List[Dict[str, Any]]:
    """POST the reference format to Draft Wizard and parse the priced pool."""
    session = requests.Session()
    session.headers['User-Agent'] = 'Mozilla/5.0'
    session.get(url, params={'sport': 'nfl'}, timeout=timeout)
    response = session.post(url, data=form_payload(config), timeout=timeout)
    response.raise_for_status()
    return parse_values(response.text)


def write_csv(rows: List[Dict[str, Any]], path: str) -> None:
    """Write the published values to CSV for cache-only board builds."""
    with open(path, 'w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, '') for col in CSV_COLUMNS})


def main(argv: Optional[List[str]] = None) -> int:
    """Refresh fp_auction_values.csv from the Draft Wizard auction calculator."""
    from config import load_config, resolve_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--out', default=None, help='defaults to paths.fp_auction_csv')
    parser.add_argument('--html', default=None, help='parse a saved page instead of fetching')
    args = parser.parse_args(argv)

    config = load_config(args.config)
    out = args.out or resolve_path(config, 'fp_auction_csv') or 'fp_auction_values.csv'
    if args.html:
        with open(args.html) as handle:
            rows = parse_values(handle.read())
    else:
        rows = fetch_values(config)
    if not rows:
        print('No auction rows parsed — the page layout may have changed.')
        return 1
    write_csv(rows, out)
    print(f"{len(rows)} FantasyPros auction values -> {out}")
    print(f"Top: " + ', '.join(f"{r['Player'].strip()} ${r['FP_Baseline']:.0f}" for r in rows[:5]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
