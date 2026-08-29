"""
draftsharks.py - Draft Sharks PPR superflex auction values as a second source.

The public page (https://www.draftsharks.com/auction-values/ppr-superflex)
renders its table server-side, but only the first 25 rows are public; the rest
sit behind a subscription. So values are kept in a CSV
(`paths.draftsharks_csv`) that this module can either scrape or read:

    python draftsharks.py                  # refresh the CSV from the public page
    python draftsharks.py --html page.html # parse a saved (logged-in) page

Columns: Player, Position, DS_Baseline (dsAuctionValue), DS_MarketValue
(auctionMarketValue), DS_Value (the 0-100 "3D value" score).

These are Draft Sharks' published dollars for THEIR default format. They are
carried as a comparison column, not blended into FP_Baseline, because the two
sources are quoted in different pots and partial coverage would silently tilt
the board toward whichever players Draft Sharks happens to publish.
"""

import argparse
import csv
import re
from typing import Any, Dict, List, Optional

import requests

PAGE_URL = 'https://www.draftsharks.com/auction-values/ppr-superflex'
ROW_RE = re.compile(r'<tbody\s+data-player-row(.*?)</tbody>', re.S)
FIELDS = ['Player', 'Position', 'DS_Baseline', 'DS_MarketValue', 'DS_Value']


def _attr(row: str, name: str) -> Optional[str]:
    match = re.search(rf'{name}="([^"]*)"', row)
    return match.group(1) if match else None


def _cell(row: str, attribute: str) -> Optional[float]:
    match = re.search(rf'data-value="\$?(-?[\d.]+)"\s+data-attribute="{attribute}"', row)
    return float(match.group(1)) if match else None


def parse_auction_values(html: str) -> List[Dict[str, Any]]:
    """Parse the auction-values table out of a Draft Sharks page."""
    rows = []
    for chunk in ROW_RE.findall(html):
        name = _attr(chunk, 'data-player-name')
        if not name:
            continue
        rows.append({
            'Player': name,
            'Position': _attr(chunk, 'data-fantasy-position') or '',
            'DS_Baseline': _cell(chunk, 'dsAuctionValue'),
            'DS_MarketValue': _cell(chunk, 'auctionMarketValue'),
            'DS_Value': _cell(chunk, 'dsValue'),
        })
    return rows


def fetch_auction_values(url: str = PAGE_URL, timeout: int = 30) -> List[Dict[str, Any]]:
    """Fetch and parse the public page (top 25 rows without a subscription)."""
    response = requests.get(url, timeout=timeout, headers={'User-Agent': 'Mozilla/5.0'})
    response.raise_for_status()
    return parse_auction_values(response.text)


def write_csv(rows: List[Dict[str, Any]], path: str) -> None:
    with open(path, 'w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description='Refresh Draft Sharks auction values.')
    parser.add_argument('--html', default=None, help='Parse a saved page instead of fetching')
    parser.add_argument('--out', default='draftsharks.csv', help='CSV to write')
    args = parser.parse_args()

    if args.html:
        with open(args.html) as handle:
            rows = parse_auction_values(handle.read())
    else:
        rows = fetch_auction_values()
    write_csv(rows, args.out)
    print(f"{len(rows)} Draft Sharks rows -> {args.out}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
