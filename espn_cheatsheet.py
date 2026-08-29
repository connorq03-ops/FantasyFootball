"""
espn_cheatsheet.py - ESPN PPR superflex auction dollars as a third source.

ESPN publishes its auction values in the Draft Kit cheat sheet PDF ("2026 ESPN
Fantasy Football Draft Kit / PPR Superflex Cheat Sheet"), four rank columns per
page, one player per entry:

    1. (QB1) Josh Allen, BUF $59 7
    85. (WR32) DK Metcalf, PIT $4 9

i.e. overall rank, positional rank, name, team, auction dollars, bye week.

    python espn_cheatsheet.py "ESPN superflex.pdf"

writes espn_baselines.csv (Player, Position, Team, ESPN_Baseline, ESPN_Rank,
Bye). Values are stored exactly as ESPN published them: ESPN quotes a $200
budget for its own league size, so its pool sums to a different pot than
FantasyPros or Draft Sharks. That level difference is absorbed downstream by
MarketScalar, never by rescaling this column.
"""

import argparse
import csv
import re
from typing import Any, Dict, List

import pdfplumber

# "12. (RB4) Jonathan Taylor, IND $44 13" - the trailing number is the bye week.
ROW_RE = re.compile(
    r'(?P<rank>\d+)\.\s+'
    r'\((?P<position>[A-Z/]+)(?P<pos_rank>\d+)\)\s+'
    r'(?P<player>[^,]+?),\s+'
    r'(?P<team>[A-Z]{2,3})\s+'
    r'\$(?P<value>\d+)\s+'
    r'(?P<bye>\d+)'
)
FIELDS = ['Player', 'Position', 'Team', 'ESPN_Baseline', 'ESPN_Rank', 'Bye']


def parse_cheatsheet(text: str) -> List[Dict[str, Any]]:
    """Parse cheat-sheet rows out of the extracted PDF text, in rank order."""
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for match in ROW_RE.finditer(text):
        rank = int(match.group('rank'))
        if rank in seen:   # the cheat sheet repeats its #1 in the header banner
            continue
        seen.add(rank)
        rows.append({
            'Player': match.group('player').strip(),
            'Position': match.group('position'),
            'Team': match.group('team'),
            'ESPN_Baseline': float(match.group('value')),
            'ESPN_Rank': rank,
            'Bye': int(match.group('bye')),
        })
    rows.sort(key=lambda row: row['ESPN_Rank'])
    return rows


def read_pdf(path: str) -> str:
    """Extract the text of every page of the cheat-sheet PDF."""
    with pdfplumber.open(path) as pdf:
        return '\n'.join(page.extract_text() or '' for page in pdf.pages)


def write_csv(rows: List[Dict[str, Any]], path: str) -> None:
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', help='ESPN Draft Kit cheat sheet PDF')
    parser.add_argument('--out', default='espn_baselines.csv')
    args = parser.parse_args()

    rows = parse_cheatsheet(read_pdf(args.pdf))
    if not rows:
        raise SystemExit('no cheat-sheet rows found - is this the ESPN Draft Kit PDF?')
    write_csv(rows, args.out)
    priced = [row for row in rows if row['ESPN_Baseline'] > 0]
    print(f'{len(rows)} players ({len(priced)} priced, '
          f'${sum(row["ESPN_Baseline"] for row in priced):.0f} total) -> {args.out}')


if __name__ == '__main__':
    main()
