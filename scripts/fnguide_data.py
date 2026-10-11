"""
FnGuide 재무제표 수집 모듈 (wcomp.fnguide.com JSON API)

2026-07 FnGuide 개편으로 comp.fnguide.com/SVO2/ASP/SVD_*.asp 가 폐기되어
신규 사이트의 JSON 엔드포인트로 이관했다.

수집 데이터:
- 연간/분기 손익계산서: 매출액, 매출총이익, 영업이익(EBIT), 당기순이익, 지배주주순이익
- 연간/분기 재무상태표: 총자산, 자본총계, 지배주주지분, 유동자산, 유동부채, 비유동자산, 차입금, 현금, 재고자산
- 연간/분기 현금흐름표: 영업활동현금흐름, 투자활동현금흐름
- Snapshot 페이지: PER, 12M PER, 업종 PER, PBR, 배당수익률(헤더), 연간 EPS

주의: 신규 API의 연간 데이터는 최근 3개년뿐이다. 그 이전 연도는
collect_quant_data.py 가 이전 캐시에서 이어붙인다.

사용법:
    from scripts.fnguide_data import get_financial_data
    data = get_financial_data('005930')
"""

import json
import os
import re
import time
from typing import Dict, List, Optional, Any

import numpy as np
import pandas as pd
import requests

# ============================================================================
# 상수
# ============================================================================

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
}

FNGUIDE_BASE = 'https://wcomp.fnguide.com/CompanyInfo'
FNGUIDE_SNAPSHOT_URL = f'{FNGUIDE_BASE}/Snapshot'

# 손익계산서 항목 매핑 (FnGuide 계정명 → 코드 키)
INCOME_ITEMS = {
    '매출액': 'revenue',
    '매출총이익': 'gross_profit',
    '영업이익': 'operating_income',
    '당기순이익': 'net_income',
    '(지배주주지분)당기순이익': 'net_income_controlling',
    '법인세비용': 'tax_expense',
    '금융원가': 'finance_cost',
}

# 재무상태표 항목 매핑 (key: 영문키, value: 검색할 계정명 리스트)
BALANCE_SEARCH = {
    'total_assets': ['자산총계', '자산'],  # 금융업은 '자산'/'자본'
    'total_equity': ['자본총계', '자본'],
    'controlling_equity': ['지배주주지분'],
    'current_assets': ['유동자산'],
    'current_liabilities': ['유동부채'],
    'non_current_assets': ['비유동자산'],
    'non_current_liabilities': ['비유동부채'],
    'cash': ['현금및현금성자산'],
    'short_term_debt': ['단기차입금'],
    'long_term_debt': ['장기차입금'],
    'bonds': ['사채'],
    'inventory': ['재고자산'],
}

# 현금흐름표 항목 매핑
CASHFLOW_ITEMS = {
    '영업활동으로인한현금흐름': 'cfo',
}

CASHFLOW_INVEST_ITEMS = ['투자활동으로인한현금흐름']

# Snapshot 페이지 헤더 지표 ID 매핑 (corp_group2 영역)
HEADER_METRIC_IDS = {
    'h_per': 'per',
    'h_12m': 'per_12m',
    'h_u_per': 'sector_per',
    'h_pbr': 'pbr',
    'h_rate': 'dividend_yield',
}


# ============================================================================
# 파싱 함수 (네트워크 없음 — test_fnguide_parse.py 에서 점검)
# ============================================================================

def _dataset_to_df(dataset: Dict, quarterly: bool) -> pd.DataFrame:
    """
    FnGuide JSON dataset → DataFrame (index=계정명, columns='YYYY/MM' 오래된→최근 순).

    - '전년동기', '전년동기대비(%)' 컬럼은 버린다.
    - 연간(quarterly=False)은 '(최근분기)' 처럼 괄호 표시가 붙은 누적 컬럼도 버린다.
      분기 표에서는 '(최근분기)' 가 실제 최신 분기이므로 유지한다.
    - 같은 이름의 계정(하위 '기타' 등)은 첫 행(상위 계정)만 남긴다.
    """
    cols = {}
    for h in dataset.get('header') or []:
        yymm = str(h.get('YYMM', ''))
        m = re.match(r'(\d{4})/(\d{2})', yymm)
        if not m or '전년' in yymm or (not quarterly and '(' in yymm):
            continue
        cols[h['CD']] = f'{m.group(1)}/{m.group(2)}'

    rows = {}
    for r in dataset.get('data') or []:
        name = re.sub(r'\s+', '', str(r.get('NAME', '')))
        if name and name not in rows:
            rows[name] = {col: r.get(cd) for cd, col in cols.items()}

    df = pd.DataFrame.from_dict(rows, orient='index', columns=list(cols.values()))
    return df.apply(pd.to_numeric, errors='coerce')


def _latest_value(series: pd.Series) -> Optional[float]:
    """가장 최근(오른쪽) 기간의 유효값."""
    valid = series.dropna()
    return float(valid.iloc[-1]) if len(valid) else None


def _parse_header_metrics(html: str) -> Dict[str, float]:
    """Snapshot 헤더에서 PER, 12M PER, 업종 PER, PBR, 배당수익률 추출."""
    metrics = {}
    pattern = r'id="(h_\w+)"[^>]*>.*?</button>\s*</li>\s*<li>\s*([^<]*)</li>'
    for match in re.finditer(pattern, html, re.DOTALL):
        key = HEADER_METRIC_IDS.get(match.group(1))
        value_str = match.group(2).replace('%', '').replace(',', '').strip()
        if key:
            try:
                metrics[key] = float(value_str)
            except ValueError:
                pass
    return metrics


def _parse_snapshot_eps(html: str) -> Dict[str, float]:
    """Snapshot 의 snpFinancial(Financial Highlight)에서 확정 연간 EPS만 추출 (추정치·분기 제외)."""
    m = re.search(r'snpFinancial\s*:\s*', html)
    if not m:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[m.end():])
    except ValueError:
        return {}
    annual_cds = {h['CD']: h['YYMM'] for h in obj.get('header', [])
                  if h.get('EP_CHK') is None and re.fullmatch(r'\d{4}/\d{2}', str(h.get('YYMM', '')))}
    for r in obj.get('data', []):
        if str(r.get('NAME', '')).strip() == 'EPS':
            out = {}
            for cd, yymm in annual_cds.items():
                try:
                    out[yymm] = float(r.get(cd))
                except (TypeError, ValueError):
                    pass
            return out
    return {}


def _extract_item(df: pd.DataFrame, item_names: list) -> pd.Series:
    """
    DataFrame에서 항목을 추출합니다.
    여러 가능한 이름을 시도합니다.
    부분 일치 시 가장 짧은 인덱스를 선택 (더 구체적인 매칭).
    """
    if df is None or df.empty:
        return pd.Series(dtype=float)

    for name in item_names:
        # 정확히 일치
        if name in df.index:
            return df.loc[name]

        # 부분 일치 (가장 짧은 매칭 = 가장 구체적)
        matches = [idx for idx in df.index if name in str(idx)]
        if matches:
            # '비지배' vs '지배' 같은 오매칭 방지: name이 idx의 시작과 일치하는 것 우선
            starts_with = [idx for idx in matches if str(idx).startswith(name)]
            if starts_with:
                return df.loc[min(starts_with, key=len)]
            return df.loc[min(matches, key=len)]

    return pd.Series(dtype=float)


# ============================================================================
# 데이터 수집 함수
# ============================================================================

def _fetch_dataset(endpoint: str, code: str, freq: str, consol: str) -> Dict:
    resp = requests.get(
        f'{FNGUIDE_BASE}/{endpoint}',
        params={'cmp_cd': code, 'freq_typ': freq, 'consol_typ': consol},
        headers=HEADERS, timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get('dataset') or {}


def _fetch_statements(code: str) -> Optional[Dict[str, pd.DataFrame]]:
    """연결(C) 우선, 비어 있으면 별도(P)로 6개 표를 받는다."""
    for consol in ('C', 'P'):
        tables = {}
        for name, endpoint in (('income', 'getFinIncome'), ('balance', 'getFinBalance'),
                               ('cashflow', 'getFinCashFlow')):
            for freq, quarterly in (('Y', False), ('Q', True)):
                tables[f'{freq}_{name}'] = _dataset_to_df(
                    _fetch_dataset(endpoint, code, freq, consol), quarterly)
        if not tables['Y_income'].dropna(how='all').empty:
            return tables
    return None


def get_financial_data(code: str, retry: int = 2) -> Optional[Dict[str, Any]]:
    """
    FnGuide에서 종목의 재무데이터를 수집합니다.

    Returns:
        {
            'code': '005930',
            'fetched_at': epoch 초,
            'header': {'per':..., 'pbr':..., 'dividend_yield':...},
            'annual': {'revenue': {'2025/12': 값, ...}, 'eps': {...}, ...},
            'quarter': {'revenue': {'2026/06': 값, ...}, ...},   # 개별 분기값
            'balance': {...},       # 최근 분기 시점값 (EV, PBR 등)
            'balance_avg': {...},   # 최근 4분기 평균 (ROE, GPA 등)
            'cashflow': {'cfo_annual': {...}, 'cfo_quarter': {...}, ...},
            'revenue_ttm': ..., 'cfo_ttm': ..., ...
        }
    """
    for attempt in range(retry):
        try:
            tables = _fetch_statements(code)
            if tables is None:
                return None

            snap = requests.get(
                FNGUIDE_SNAPSHOT_URL,
                params={'c_id': 'AA', 'menu_type': '01', 'cmp_cd': code},
                headers=HEADERS, timeout=20,
            )
            snap.raise_for_status()

            result = {
                'code': code,
                'fetched_at': time.time(),
                'header': _parse_header_metrics(snap.text),
                'annual': {},
                'quarter': {},
                'balance': {},
                'balance_avg': {},
                'cashflow': {},
            }

            # 1. 손익계산서
            for period, df in (('annual', tables['Y_income']), ('quarter', tables['Q_income'])):
                for kr_name, en_key in INCOME_ITEMS.items():
                    series = _extract_item(df, [kr_name]).dropna()
                    if not series.empty:
                        result[period][en_key] = series.to_dict()

            eps = _parse_snapshot_eps(snap.text)
            if eps:
                result['annual']['eps'] = eps

            # 2. 재무상태표 (분기 우선, 없으면 연간). 컬럼은 오래된→최근 순
            bs = tables['Q_balance'] if not tables['Q_balance'].dropna(how='all').empty else tables['Y_balance']
            for en_key, kr_names in BALANCE_SEARCH.items():
                series = _extract_item(bs, kr_names).dropna()
                if not series.empty:
                    result['balance'][en_key] = _latest_value(series)
                    result['balance_avg'][en_key] = float(series.tail(4).mean())

            result['balance']['total_debt'] = sum(
                result['balance'].get(k) or 0 for k in ('short_term_debt', 'long_term_debt', 'bonds'))

            # 3. 현금흐름표
            for suffix, df in (('annual', tables['Y_cashflow']), ('quarter', tables['Q_cashflow'])):
                cfo = _extract_item(df, list(CASHFLOW_ITEMS)).dropna()
                if not cfo.empty:
                    result['cashflow'][f'cfo_{suffix}'] = cfo.to_dict()
                inv = _extract_item(df, CASHFLOW_INVEST_ITEMS).dropna()
                if not inv.empty:
                    result['cashflow'][f'invest_cf_{suffix}'] = inv.to_dict()

            _calculate_ttm(result)
            return result

        except Exception:
            if attempt < retry - 1:
                time.sleep(1)

    return None


def _calculate_ttm(data: Dict[str, Any]) -> None:
    """
    TTM (Trailing Twelve Months) 계산.
    분기 데이터가 4개 이상 있으면 최근 4분기 합산.
    없으면 최신 연간 데이터 사용.
    """
    for key in ['revenue', 'operating_income', 'net_income', 'gross_profit',
                 'net_income_controlling', 'tax_expense', 'finance_cost']:
        annual = data['annual'].get(key, {})
        quarter = data['quarter'].get(key, {})

        ttm_value = None

        # 분기 데이터로 TTM 시도
        if len(quarter) >= 4:
            # 날짜순 정렬 (최신 먼저)
            sorted_quarters = sorted(quarter.items(), key=lambda x: x[0], reverse=True)
            recent_4 = [v for _, v in sorted_quarters[:4] if v is not None and not np.isnan(v)]
            if len(recent_4) == 4:
                ttm_value = sum(recent_4)

        # TTM 불가 → 최신 연간 데이터
        if ttm_value is None and annual:
            sorted_annual = sorted(annual.items(), key=lambda x: x[0], reverse=True)
            for _, v in sorted_annual:
                if v is not None and not np.isnan(v):
                    ttm_value = v
                    break

        if ttm_value is not None:
            data[f'{key}_ttm'] = ttm_value

    # CFO TTM
    cfo_annual = data['cashflow'].get('cfo_annual', {})
    cfo_quarter = data['cashflow'].get('cfo_quarter', {})

    cfo_ttm = None
    if len(cfo_quarter) >= 4:
        sorted_q = sorted(cfo_quarter.items(), key=lambda x: x[0], reverse=True)
        recent_4 = [v for _, v in sorted_q[:4] if v is not None and not np.isnan(v)]
        if len(recent_4) == 4:
            cfo_ttm = sum(recent_4)

    if cfo_ttm is None and cfo_annual:
        sorted_a = sorted(cfo_annual.items(), key=lambda x: x[0], reverse=True)
        for _, v in sorted_a:
            if v is not None and not np.isnan(v):
                cfo_ttm = v
                break

    if cfo_ttm is not None:
        data['cfo_ttm'] = cfo_ttm

    # 투자활동CF TTM
    inv_annual = data['cashflow'].get('invest_cf_annual', {})
    inv_quarter = data['cashflow'].get('invest_cf_quarter', {})

    inv_ttm = None
    if len(inv_quarter) >= 4:
        sorted_q = sorted(inv_quarter.items(), key=lambda x: x[0], reverse=True)
        recent_4 = [v for _, v in sorted_q[:4] if v is not None and not np.isnan(v)]
        if len(recent_4) == 4:
            inv_ttm = sum(recent_4)

    if inv_ttm is None and inv_annual:
        sorted_a = sorted(inv_annual.items(), key=lambda x: x[0], reverse=True)
        for _, v in sorted_a:
            if v is not None and not np.isnan(v):
                inv_ttm = v
                break

    if inv_ttm is not None:
        data['invest_cf_ttm'] = inv_ttm


# ============================================================================
# 배치 수집
# ============================================================================

def get_financial_data_batch(codes: List[str], delay: float = 0.3) -> Dict[str, Dict]:
    """
    여러 종목의 재무데이터를 순차 수집합니다.

    Args:
        codes: 종목코드 리스트
        delay: 요청 간 대기시간 (FnGuide 속도 제한으로 병렬 처리 불가)

    Returns:
        {code: financial_data} 딕셔너리
    """
    results = {}
    total = len(codes)

    print(f"FnGuide 재무데이터 수집 시작 ({total}개 종목)")

    for i, code in enumerate(codes):
        data = get_financial_data(code)
        if data:
            results[code] = data

        if (i + 1) % 50 == 0 or (i + 1) == total:
            print(f"  진행: {i + 1}/{total} (수집: {len(results)}개)")

        if delay > 0:
            time.sleep(delay)

    print(f"수집 완료: {len(results)}/{total}개")
    return results


# ============================================================================
# 헬퍼 함수 (스크리너에서 사용)
# ============================================================================

def get_ebit(data: Dict) -> Optional[float]:
    """
    EBIT = 영업이익 (Operating Income, TTM)
    K-IFRS '영업이익'이 그린블라트의 EBIT과 동일.
    NI+Tax+FC는 비영업수익(금융수익, 자산매각 등)을 포함하므로 사용하지 않음.
    """
    op = data.get('operating_income_ttm')
    if op is not None:
        return op

    # fallback: NI + Tax + Finance Cost (영업이익 데이터 없을 때만)
    ni = data.get('net_income_ttm')
    tax = data.get('tax_expense_ttm')
    fin_cost = data.get('finance_cost_ttm')

    if ni is not None and tax is not None and fin_cost is not None:
        return ni + tax + fin_cost

    return None


def get_invested_capital(data: Dict) -> Optional[float]:
    """투하자본 = 유동자산 - 유동부채 + 비유동자산 (4분기 평균)"""
    bs = data.get('balance_avg') or data.get('balance', {})
    ca = bs.get('current_assets')
    cl = bs.get('current_liabilities')
    nca = bs.get('non_current_assets')

    if ca is not None and cl is not None and nca is not None:
        return ca - cl + nca
    return None


def get_ev(market_cap: float, data: Dict) -> Optional[float]:
    """
    기업가치 (EV) = 시가총액 + 총부채 - 여유자금
    총부채 = 유동부채 + 비유동부채
    여유자금 = 현금 - MAX(0, 유동부채 - 유동자산 + 현금)
    현금 데이터 없으면 여유자금=0으로 처리
    """
    bs = data.get('balance', {})
    cl = bs.get('current_liabilities', 0) or 0
    ncl = bs.get('non_current_liabilities', 0) or 0
    total_liabilities = cl + ncl

    # 여유자금 계산
    cash = bs.get('cash', 0) or 0
    ca = bs.get('current_assets', 0) or 0
    if cash > 0:
        working_capital_need = max(0, cl - ca + cash)
        excess_cash = cash - working_capital_need
    else:
        excess_cash = 0

    ev = market_cap + total_liabilities - excess_cash
    return ev if ev > 0 else market_cap


def get_roe(data: Dict) -> Optional[float]:
    """ROE = 지배주주순이익(TTM) / 지배주주지분(4분기 평균)"""
    ni = data.get('net_income_controlling_ttm') or data.get('net_income_ttm')
    bs = data.get('balance_avg') or data.get('balance', {})
    eq = bs.get('controlling_equity') or bs.get('total_equity')

    if ni is not None and eq is not None and eq > 0:
        return (ni / eq) * 100
    return None


def get_gpa(data: Dict) -> Optional[float]:
    """GPA = 매출총이익(TTM) / 총자산(4분기 평균)"""
    gp = data.get('gross_profit_ttm')
    bs = data.get('balance_avg') or data.get('balance', {})
    ta = bs.get('total_assets')

    if gp is not None and ta is not None and ta > 0:
        return gp / ta
    return None


def get_cfo_ratio(data: Dict) -> Optional[float]:
    """CFO = 영업활동현금흐름(TTM) / 총자산(4분기 평균)"""
    cfo = data.get('cfo_ttm')
    bs = data.get('balance_avg') or data.get('balance', {})
    ta = bs.get('total_assets')

    if cfo is not None and ta is not None and ta > 0:
        return cfo / ta
    return None


def get_psr(market_cap: float, data: Dict) -> Optional[float]:
    """PSR = 시가총액 / 매출액(TTM)"""
    rev = data.get('revenue_ttm')
    if rev is not None and rev > 0:
        return market_cap / rev
    return None


def get_pcr(market_cap: float, data: Dict) -> Optional[float]:
    """PCR = 시가총액 / 영업현금흐름(TTM)"""
    cfo = data.get('cfo_ttm')
    if cfo is not None and cfo > 0:
        return market_cap / cfo
    return None


def get_current_ratio(data: Dict) -> Optional[float]:
    """유동비율 = 유동자산 / 유동부채 (배수)"""
    bs = data.get('balance', {})
    ca = bs.get('current_assets')
    cl = bs.get('current_liabilities')
    if ca is not None and cl is not None and cl > 0:
        return ca / cl
    return None


def get_debt_ratio(data: Dict) -> Optional[float]:
    """부채비율 = (유동부채+비유동부채) / 자본총계 × 100"""
    bs = data.get('balance', {})
    cl = bs.get('current_liabilities', 0) or 0
    ncl = bs.get('non_current_liabilities', 0) or 0
    eq = bs.get('total_equity')
    if eq is not None and eq > 0:
        return ((cl + ncl) / eq) * 100
    return None


def get_long_term_debt_ratio(data: Dict) -> Optional[float]:
    """장기부채비율 = (장기차입금+사채) / 자본총계 × 100"""
    bs = data.get('balance', {})
    long_debt = bs.get('long_term_debt', 0) or 0
    bonds = bs.get('bonds', 0) or 0
    eq = bs.get('total_equity')
    if eq is not None and eq > 0 and (long_debt + bonds) > 0:
        return ((long_debt + bonds) / eq) * 100
    return None


def get_roa(data: Dict) -> Optional[float]:
    """ROA = 순이익(TTM) / 총자산(4분기 평균) × 100"""
    ni = data.get('net_income_ttm')
    bs = data.get('balance_avg') or data.get('balance', {})
    ta = bs.get('total_assets')
    if ni is not None and ta is not None and ta > 0:
        return (ni / ta) * 100
    return None


def get_roic(data: Dict) -> Optional[float]:
    """ROIC = EBIT / 투하자본 × 100"""
    ebit = get_ebit(data)
    ic = get_invested_capital(data)
    if ebit is not None and ic is not None and ic > 0:
        return (ebit / ic) * 100
    return None


def get_net_current_assets(data: Dict) -> Optional[float]:
    """순유동자산 = 유동자산 - 유동부채 (억원)"""
    bs = data.get('balance', {})
    ca = bs.get('current_assets')
    cl = bs.get('current_liabilities')
    if ca is not None and cl is not None:
        return ca - cl
    return None


def get_fcf(data: Dict) -> Optional[float]:
    """FCF = CFO(TTM) + 투자활동CF(TTM)  (투자CF는 음수이므로 + 연산)"""
    cfo = data.get('cfo_ttm')
    inv_cf = data.get('invest_cf_ttm')
    if cfo is not None and inv_cf is not None:
        return cfo + inv_cf
    return None


def get_inventory_ratio(data: Dict) -> Optional[float]:
    """재고/매출 비율 = 재고자산(4Q평균) / 매출액(TTM) × 100"""
    bs = data.get('balance_avg') or data.get('balance', {})
    inv = bs.get('inventory')
    rev = data.get('revenue_ttm')
    if inv is not None and rev is not None and rev > 0:
        return (inv / rev) * 100
    return None


def get_peg(market_cap: float, data: Dict) -> Optional[float]:
    """PEG = PER / EPS성장률(3년평균). 성장률이 음수면 None."""
    per = get_per_from_data(market_cap, data)
    if per is None or per <= 0:
        return None

    growth_rates = get_annual_growth_rates(data, 'eps', years=3)
    if not growth_rates:  # eps 데이터 없으면 net_income으로 fallback (구버전 캐시 호환)
        growth_rates = get_growth_rates_with_ttm(data, 'net_income', years=3)
    valid_rates = [r for r in growth_rates if r is not None and r > 0]
    if not valid_rates:
        return None

    avg_growth = sum(valid_rates) / len(valid_rates)
    if avg_growth <= 0:
        return None

    return per / avg_growth


# ============================================================================
# 캐시 저장/로드
# ============================================================================

def save_fnguide_cache(results: Dict[str, Dict], filepath: str) -> None:
    """FnGuide 수집 결과를 pickle 캐시로 저장"""
    import pickle
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'wb') as f:
        pickle.dump(results, f, protocol=pickle.HIGHEST_PROTOCOL)
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    print(f"캐시 저장: {filepath} ({len(results)}개 종목, {size_mb:.1f}MB)")


def load_fnguide_cache(filepath: str = None) -> Optional[Dict[str, Dict]]:
    """
    FnGuide pickle 캐시 로드.
    filepath이 None이면 .cache/ 에서 최신 fnguide_*.pkl 파일을 찾음.
    """
    import pickle
    import glob as glob_mod

    if filepath and os.path.exists(filepath):
        try:
            with open(filepath, 'rb') as f:
                data = pickle.load(f)
            print(f"캐시 로드: {filepath} ({len(data)}개 종목)")
            return data
        except Exception as e:
            print(f"캐시 로드 실패: {e}")
            return None

    # filepath이 없으면 .cache/에서 최신 파일 탐색
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_dir = os.path.join(script_dir, '.cache')
    if not os.path.exists(cache_dir):
        return None

    pkl_files = sorted(
        glob_mod.glob(os.path.join(cache_dir, 'fnguide_*.pkl')),
        reverse=True
    )
    for pkl_file in pkl_files:
        try:
            with open(pkl_file, 'rb') as f:
                data = pickle.load(f)
            print(f"캐시 로드: {pkl_file} ({len(data)}개 종목)")
            return data
        except Exception:
            continue

    return None


# ============================================================================
# 성장률/마진 헬퍼 (test_value_screener.py 등에서 사용)
# ============================================================================

def get_growth_rates_with_ttm(data: Dict, key: str, years: int = 5) -> List[Optional[float]]:
    """
    TTM 값 + 연간 데이터로 YoY 성장률 시리즈를 구성.

    반환: [TTM/최신연간, 최신연간/전년, 전년/전전년, ...]

    Args:
        data: get_financial_data() 결과
        key: 'revenue', 'operating_income', 'net_income' 등
        years: 최대 성장률 개수
    """
    ttm = data.get(f'{key}_ttm')
    annual = data.get('annual', {}).get(key, {})

    # 연도 역순 정렬 (최신 먼저)
    sorted_annual = sorted(annual.items(), key=lambda x: x[0], reverse=True)
    annual_values = [v for _, v in sorted_annual]

    # TTM + 연간 값 결합
    if ttm is not None:
        values = [ttm] + annual_values
    else:
        values = annual_values

    # YoY 성장률 계산
    rates = []
    for i in range(min(years, len(values) - 1)):
        curr, prev = values[i], values[i + 1]
        if curr is not None and prev is not None and prev != 0 and not np.isnan(curr) and not np.isnan(prev):
            rates.append(round(((curr / prev) - 1) * 100, 2))
        else:
            rates.append(None)

    return rates


def get_annual_growth_rates(data: Dict, key: str, years: int = 5) -> List[Optional[float]]:
    """
    연간 데이터만으로 YoY 성장률 계산 (TTM 미포함).

    반환: [최신연간/전년, 전년/전전년, ...]
    """
    annual = data.get('annual', {}).get(key, {})
    sorted_annual = sorted(annual.items(), key=lambda x: x[0], reverse=True)
    values = [v for _, v in sorted_annual]

    rates = []
    for i in range(min(years, len(values) - 1)):
        curr, prev = values[i], values[i + 1]
        if curr is not None and prev is not None and prev != 0 and not np.isnan(curr) and not np.isnan(prev):
            rates.append(round(((curr / prev) - 1) * 100, 2))
        else:
            rates.append(None)

    return rates


def get_operating_margins(data: Dict, years: int = 5) -> List[Optional[float]]:
    """
    영업이익률 시리즈 반환 (TTM + 연간).

    반환: [TTM 영업이익률, 최신연간, 전년, ...]
    """
    margins = []

    # TTM 영업이익률
    rev_ttm = data.get('revenue_ttm')
    op_ttm = data.get('operating_income_ttm')
    if rev_ttm and op_ttm and rev_ttm != 0:
        margins.append(round((op_ttm / rev_ttm) * 100, 2))

    # 연간 영업이익률
    annual_rev = data.get('annual', {}).get('revenue', {})
    annual_op = data.get('annual', {}).get('operating_income', {})

    sorted_years = sorted(annual_rev.keys(), reverse=True)
    for year in sorted_years[:years]:
        rev = annual_rev.get(year)
        op = annual_op.get(year)
        if rev and op and rev != 0:
            margins.append(round((op / rev) * 100, 2))
        else:
            margins.append(None)

    return margins[:years + 1]  # TTM + years개


def get_per_from_data(market_cap: float, data: Dict) -> Optional[float]:
    """PER: FnGuide 헤더값 우선, 없으면 시가총액/순이익TTM으로 계산"""
    header_per = data.get('header', {}).get('per')
    if header_per is not None and header_per > 0:
        return round(header_per, 2)
    ni = data.get('net_income_controlling_ttm') or data.get('net_income_ttm')
    if ni is not None and ni > 0 and market_cap > 0:
        return round(market_cap / ni, 2)
    return None


def get_pbr_from_data(market_cap: float, data: Dict) -> Optional[float]:
    """PBR: FnGuide 헤더값 우선, 없으면 시가총액/자본총계로 계산"""
    header_pbr = data.get('header', {}).get('pbr')
    if header_pbr is not None and header_pbr > 0:
        return round(header_pbr, 2)
    bs = data.get('balance', {})
    eq = bs.get('controlling_equity') or bs.get('total_equity')
    if eq is not None and eq > 0 and market_cap > 0:
        return round(market_cap / eq, 2)
    return None


if __name__ == '__main__':
    # 테스트: 삼성전자
    print("=" * 60)
    print("FnGuide 재무데이터 테스트: 삼성전자 (005930)")
    print("=" * 60)

    data = get_financial_data('005930')
    if data:
        print(f"\n연간 손익계산서:")
        for key, values in data.get('annual', {}).items():
            print(f"  {key}: {values}")

        print(f"\nTTM 데이터:")
        for key in ['revenue_ttm', 'operating_income_ttm', 'net_income_ttm',
                     'gross_profit_ttm', 'tax_expense_ttm', 'finance_cost_ttm', 'cfo_ttm']:
            print(f"  {key}: {data.get(key)}")
        print(f"\nEBIT (영업이익 TTM): {get_ebit(data)}")

        print(f"\n재무상태표:")
        for key, value in data.get('balance', {}).items():
            print(f"  {key}: {value}")

        print(f"\n투하자본: {get_invested_capital(data)}")
        print(f"ROE: {get_roe(data)}")
        print(f"GPA: {get_gpa(data)}")
        print(f"CFO ratio: {get_cfo_ratio(data)}")
    else:
        print("데이터 수집 실패")
