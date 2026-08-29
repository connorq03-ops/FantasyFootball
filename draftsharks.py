"""
draftsharks.py - Draft Sharks PPR superflex auction values as a second source.

https://www.draftsharks.com/auction-values/ppr-superflex is the SECOND
baseline. A plain HTTP GET only returns the first 25 rows — the remaining ~225
are lazy-loaded as you scroll — so the full table is captured by driving the
browser over CDP and scrolling until the row count stops growing:

    python draftsharks.py                  # browser capture (all ~250 rows)
    python draftsharks.py --no-browser     # plain GET (top 25 only)
    python draftsharks.py --html page.html # parse a saved page

Columns: Player, Position, DS_MarketValue (auctionMarketValue), DS_Value (the
0-100 "3D value" score). DS_MarketValue is the average auction value across a
consensus of 30+ sites for this scoring format, which is the number used as the
baseline; their own model's dsAuctionValue is deliberately not carried.

Draft Sharks quotes full-PPR, $200-budget superflex dollars for its own league
size, so its priced pool sums to a bigger pot than the FantasyPros reference
(~$2,500 over 250 players vs $2,000 over 148). The column is stored exactly as
published — Avg_Baseline averages the sites and MarketScalar reconciles the
level difference against this league's actual pot.
"""

import argparse
import asyncio
import csv
import re
from typing import Any, Dict, List, Optional

import requests
from playwright.async_api import async_playwright

PAGE_URL = 'https://www.draftsharks.com/auction-values/ppr-superflex'
CDP_URL = 'http://localhost:29229'
ROW_RE = re.compile(r'<tbody\s+data-player-row(.*?)</tbody>', re.S)
FIELDS = ['Player', 'Position', 'DS_MarketValue', 'DS_Value']


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
            'DS_MarketValue': _cell(chunk, 'auctionMarketValue'),
            'DS_Value': _cell(chunk, 'dsValue'),
        })
    return rows


def fetch_auction_values(url: str = PAGE_URL, timeout: int = 30) -> List[Dict[str, Any]]:
    """Fetch and parse the page over HTTP (returns only the first 25 rows)."""
    response = requests.get(url, timeout=timeout, headers={'User-Agent': 'Mozilla/5.0'})
    response.raise_for_status()
    return parse_auction_values(response.text)


async def _scroll_for_html(url: str, cdp_url: str, max_scrolls: int) -> str:
    async with async_playwright() as driver:
        browser = await driver.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=90000)
            rows = page.locator('tbody[data-player-row]')
            previous = -1
            for _ in range(max_scrolls):
                await page.wait_for_timeout(1500)
                count = await rows.count()
                if count == previous:
                    break
                previous = count
                await page.mouse.wheel(0, 20000)
            return await page.content()
        finally:
            await page.close()


def fetch_auction_values_browser(url: str = PAGE_URL, cdp_url: str = CDP_URL,
                                 max_scrolls: int = 40) -> List[Dict[str, Any]]:
    """Scroll the page in the real browser so every lazy-loaded row is rendered."""
    return parse_auction_values(asyncio.run(_scroll_for_html(url, cdp_url, max_scrolls)))


def write_csv(rows: List[Dict[str, Any]], path: str) -> None:
    with open(path, 'w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description='Refresh Draft Sharks auction values.')
    parser.add_argument('--html', default=None, help='Parse a saved page instead of fetching')
    parser.add_argument('--out', default='draftsharks.csv', help='CSV to write')
    parser.add_argument('--no-browser', action='store_true',
                        help='Plain HTTP GET (top 25 rows only) instead of a browser capture')
    parser.add_argument('--cdp', default=CDP_URL, help='Chrome DevTools endpoint')
    args = parser.parse_args()

    if args.html:
        with open(args.html) as handle:
            rows = parse_auction_values(handle.read())
    elif args.no_browser:
        rows = fetch_auction_values()
    else:
        rows = fetch_auction_values_browser(cdp_url=args.cdp)
    if not rows:
        print('No Draft Sharks rows parsed — the page layout may have changed.')
        return 1
    write_csv(rows, args.out)
    print(f"{len(rows)} Draft Sharks rows -> {args.out}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
