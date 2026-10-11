"""
FnGuide 신규 API(wcomp.fnguide.com) 파싱 로직 점검 — 네트워크 없이 실행.

    python test_fnguide_parse.py
"""
from scripts.fnguide_data import (
    _dataset_to_df, _extract_item, _latest_value, _parse_header_metrics, _parse_snapshot_eps,
)

# 분기 손익: VAL1=가장 오래된 분기 … VAL4=최근 분기, VAL5=전년동기, VAL6=증감률
Q_INCOME = {
    'header': [
        {'YYMM': '2025/09', 'CD': 'VAL1'}, {'YYMM': '2025/12', 'CD': 'VAL2'},
        {'YYMM': '2026/03', 'CD': 'VAL3'}, {'YYMM': '2026/06 (최근분기)', 'CD': 'VAL4'},
        {'YYMM': '2025/06 (전년동기)', 'CD': 'VAL5'}, {'YYMM': '전년동기대비(%)', 'CD': 'VAL6'},
    ],
    'data': [
        {'NAME': '매출액(수익)', 'VAL1': '10', 'VAL2': '20', 'VAL3': '30', 'VAL4': '40', 'VAL5': '5', 'VAL6': '700'},
        {'NAME': '    기타', 'VAL1': '1', 'VAL2': '1', 'VAL3': '1', 'VAL4': '1', 'VAL5': '1', 'VAL6': '0'},
        {'NAME': '    기타', 'VAL1': '9', 'VAL2': '9', 'VAL3': '9', 'VAL4': '9', 'VAL5': '9', 'VAL6': '0'},
        {'NAME': '(지배주주지분)당기순이익', 'VAL1': '1', 'VAL2': None, 'VAL3': '3', 'VAL4': '4'},
    ],
}

# 연간 손익: 마지막 두 컬럼은 반기 누적·전년동기 → 연간 시계열에서 제외해야 함
Y_INCOME = {
    'header': [
        {'YYMM': '2023/12', 'CD': 'VAL1'}, {'YYMM': '2024/12', 'CD': 'VAL2'},
        {'YYMM': '2025/12', 'CD': 'VAL3'}, {'YYMM': '2026/06 (최근분기)', 'CD': 'VAL4'},
        {'YYMM': '2025/06 (전년동기)', 'CD': 'VAL5'},
    ],
    'data': [{'NAME': '매출액(수익)', 'VAL1': '100', 'VAL2': '200', 'VAL3': '300', 'VAL4': '150', 'VAL5': '90'}],
}

q = _dataset_to_df(Q_INCOME, quarterly=True)
assert list(q.columns) == ['2025/09', '2025/12', '2026/03', '2026/06'], q.columns
assert _extract_item(q, ['매출액']).to_dict() == {'2025/09': 10, '2025/12': 20, '2026/03': 30, '2026/06': 40}
assert _extract_item(q, ['기타']).iloc[0] == 1, '중복 계정명은 첫 행(상위 계정)을 써야 함'
assert _latest_value(_extract_item(q, ['매출액'])) == 40, '최신값은 가장 오른쪽(최근) 분기'
assert _latest_value(_extract_item(q, ['(지배주주지분)당기순이익'])) == 4

y = _dataset_to_df(Y_INCOME, quarterly=False)
assert list(y.columns) == ['2023/12', '2024/12', '2025/12'], y.columns

HEADER_HTML = '''
<button type="button" class="tip_in" id="h_per">PER</button> </li> <li>39.92 </li> </ul>
<button type="button" class="tip_in" id="h_12m">12M PER</button> </li> <li>- </li> </ul>
<button type="button" class="tip_in" id="h_pbr">PBR</button> </li> <li>4.09 </li> </ul>
<button type="button" class="tip_in" id="h_rate"><span class="ko">배당수익률</span></button> </li> <li>0.64 % </li>
'''
assert _parse_header_metrics(HEADER_HTML) == {'per': 39.92, 'pbr': 4.09, 'dividend_yield': 0.64}

SNAP_HTML = 'snpFinancial: {"header":[{"YYMM":"2024/12","EP_CHK":null,"CD":"VAL1"},' \
    '{"YYMM":"2025/12","EP_CHK":null,"CD":"VAL2"},{"YYMM":"2026/12","EP_CHK":"E","CD":"VAL3"},' \
    '{"YYMM":"2026/06","EP_CHK":" ","CD":"VAL4"}],' \
    '"data":[{"NAME":"EPS","VAL1":"4949.65","VAL2":"6563.57","VAL3":"45418.6","VAL4":"10717.8"}]},'
assert _parse_snapshot_eps(SNAP_HTML) == {'2024/12': 4949.65, '2025/12': 6563.57}, '추정치(E)·분기 제외'

from collect_quant_data import merge_older_annual
new = {'annual': {'revenue': {'2023/12': 1, '2024/12': 2, '2025/12': 3}}}
merge_older_annual(new, {'annual': {'revenue': {'2021/12': 9, '2022/12': 8, '2024/12': 999},
                                    'eps': {'2022/12': 5}}})
assert new['annual']['revenue'] == {'2021/12': 9, '2022/12': 8, '2023/12': 1, '2024/12': 2, '2025/12': 3}, '최신값 덮어쓰기 금지'
assert new['annual']['eps'] == {'2022/12': 5}

print('OK: FnGuide 파싱 점검 통과')
