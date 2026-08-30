"""Google Sheets live-auction worksheet generation."""

from typing import List

import pandas as pd

from names import normalized_key

LIVE_SHEET_COLUMNS = [
    'Ranking', 'Player', 'Position', 'Team', 'Bye', 'Low', 'Target', 'Exit',
    'Tier', 'PosRankByAdj', 'FinalAdj', 'ExpMarketPrice', 'Delta v. Expected',
    'Delta v. ESPN', 'Delta v. AVG', 'FP_Baseline', 'DS_MarketValue',
    'ESPN_Baseline', '', 'What Went For', 'Won?', 'Status', '', 'BaseLow',
    'BaseTarget', 'BaseExit', 'BaseAdj', 'BaseMarket', 'Setting', 'Value',
]


def _target_value(target: pd.Series, column: str) -> float:
    """Return a numeric target value, defaulting missing values to zero."""
    value = target.get(column, 0)
    return 0 if pd.isna(value) else value


def build_live_sheet(board: pd.DataFrame, targets: pd.DataFrame,
                     remaining_pot: float, spend_rate: float, board_floor: int,
                     my_budget: float, my_slots: int) -> pd.DataFrame:
    """
    Build the formula-driven live auction worksheet from completed board data.

    The returned frame uses placeholder names for the two final status columns;
    the writer replaces those two header cells with the row-one status entry.
    """
    priced = board[(board['IsAvailable'] == 1) & (board['InDraftPool'] == 1)]
    priced = priced.sort_values('FinalAdj', ascending=False).reset_index(drop=True)
    board_names = set(priced['Player'].map(normalized_key))
    target_by_key = {}
    unmatched: List[str] = []
    for _, target in targets.iterrows():
        name = str(target.get('Player', '')).strip()
        key = normalized_key(name)
        if key not in board_names:
            unmatched.append(name)
        elif key not in target_by_key:
            target_by_key[key] = target
    if unmatched:
        print('Warning: unmatched targets: ' + ', '.join(unmatched))

    last = len(priced) + 1
    rows = []
    for index, player in priced.iterrows():
        row_number = index + 2
        target = target_by_key.get(normalized_key(player['Player']))
        if target is None:
            low = target_value = exit_value = 0
        else:
            low = _target_value(target, 'Low')
            target_value = _target_value(target, 'Target')
            exit_value = _target_value(target, 'Exit')
        rows.append([
            index + 1,
            player['Player'],
            player['Position'],
            player['Team'],
            player['Bye'],
            f'=IF($T{row_number}<>"","",ROUND($X{row_number}*IF($AA{row_number}>0,$K{row_number}/$AA{row_number},1)))',
            f'=IF($T{row_number}<>"","",ROUND($Y{row_number}*IF($AA{row_number}>0,$K{row_number}/$AA{row_number},1)))',
            f'=IF($T{row_number}<>"","",ROUND($Z{row_number}*IF($AA{row_number}>0,$K{row_number}/$AA{row_number},1)))',
            player['Tier'],
            player['PosRankByAdj'],
            f'=IF($T{row_number}<>"","",ROUND({board_floor}+$AD$7*($AA{row_number}-{board_floor}),1))',
            f'=IF($T{row_number}<>"","",ROUND({board_floor}+$AD$10*($AB{row_number}-{board_floor}),1))',
            f'=IF($T{row_number}<>"","",$K{row_number}-$L{row_number})',
            f'=IF($T{row_number}<>"","",$K{row_number}-$R{row_number})',
            f'=IF($T{row_number}<>"","",ROUND($K{row_number}-AVERAGE($P{row_number},$Q{row_number},$R{row_number}),1))',
            player['FP_Baseline'],
            player['DS_MarketValue'],
            player['ESPN_Baseline'],
            '',
            '',
            False,
            f'=IF($T{row_number}="","AVAIL",IF($U{row_number}=TRUE,"MINE","SOLD"))',
            '',
            low,
            target_value,
            exit_value,
            player['FinalAdj'],
            player['MarketPrice'],
            '',
            '',
        ])

    status_rows = [
        ('Dollars spent', f'=SUM($T$2:$T${last})'),
        ('Pot left', '=$AD$1-$AD$2'),
        ('Players left', f'=COUNTIFS($T$2:$T${last},"")'),
        ('Base value left', f'=SUMIFS($AA$2:$AA${last},$T$2:$T${last},"")'),
        ('Base market left', f'=SUMIFS($AB$2:$AB${last},$T$2:$T${last},"")'),
        ('Value scalar', ''),
        ('Inflation', '=$AD$7-1'),
        ('Market pot left', f'=$AD$3*{spend_rate}'),
        ('Market scalar', ''),
        ('My budget at start', my_budget),
        ('My budget left', f'=$AD$11-SUMIFS($T$2:$T${last},$U$2:$U${last},TRUE)'),
        ('My slots left', f'={my_slots}-COUNTIFS($U$2:$U${last},TRUE)'),
        ('My max bid now', f'=MAX({board_floor},$AD$12-($AD$13-{board_floor}))'),
        ('Check: live value total', f'=SUMIFS($K$2:$K${last},$T$2:$T${last},"")'),
    ]
    floor_count = '$AD$4' if board_floor == 1 else f'$AD$4*{board_floor}'
    status_rows[5] = ('Value scalar', (
        f'=IF($AD$5-{floor_count}<=0,1,'
        f'MAX(0,($AD$3-{floor_count})/'
        f'($AD$5-{floor_count})))'
    ))
    status_rows[8] = ('Market scalar', (
        f'=IF($AD$6-{floor_count}<=0,1,'
        f'MAX(0,($AD$9-{floor_count})/'
        f'($AD$6-{floor_count})))'
    ))
    for index, (label, value) in enumerate(status_rows):
        if index < len(rows):
            rows[index][28] = label
            rows[index][29] = value

    return pd.DataFrame(rows, columns=LIVE_SHEET_COLUMNS)


def live_sheet_headers(remaining_pot: float) -> List[str]:
    """Return CSV headers with the first status entry in columns AC and AD."""
    headers = LIVE_SHEET_COLUMNS.copy()
    headers[28] = 'Pot at start'
    headers[29] = str(int(remaining_pot))
    return headers
