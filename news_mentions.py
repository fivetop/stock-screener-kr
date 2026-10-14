"""
뉴스 언급량 수집기 (Google News RSS)

스크리너가 뽑은 종목들이 일간지·경제지에서 얼마나 거론됐는지 집계한다.
키가 필요 없는 Google News RSS 를 사용하며, 종목코드로 검색해 정확도를 높인다.

출력: data/news_mentions.json
사용:
    python news_mentions.py                 # 스크리너 결과에서 유니버스 자동 구성
    python news_mentions.py 079650 278650   # 특정 종목코드만
    python news_mentions.py --limit 50      # 최대 종목 수 제한
    python news_mentions.py --no-name       # 이름 검색 생략 (코드 검색만)

주의:
    - RSS 는 정확한 총건수가 아니라 근사치다 (쿼리당 최대 약 100건).
    - 이름 검색은 동음이의 오염이 있어 주가 키워드가 있는 기사만 채택한다.
"""

import os
import re
import sys
import json
import time
import argparse
import datetime
import email.utils
import xml.etree.ElementTree as ET

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, 'data')
OUTPUT_FILE = os.path.join(DATA_DIR, 'news_mentions.json')

RSS_ENDPOINT = 'https://news.google.com/rss/search'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; stock-screener-kr news counter)'}
REQ_DELAY = float(os.environ.get('NEWS_REQ_DELAY', '1.0'))

# 유니버스로 삼을 스크리너 JSON (관심도 높은 종목 위주)
SOURCE_FILES = [
    'price_rise.json',
    'volume_explosion.json',
    'new_high_52w.json',
    'near_high_52w.json',
    'box_breakout.json',
    'box_breakout_simple.json',
    'fallen_rebound.json',
    'bottom_breakout.json',
]
MAX_STOCKS = 200           # RSS 부하 제한
SAMPLE_LIMIT = 5           # JSON 에 저장할 대표 기사 수

# 이름 검색 결과에서 '주식 기사'만 남기기 위한 키워드 (오탐 방지 위해 정밀도 높은 것만)
STOCK_HINTS = (
    '주가', '급등', '급락', '상한가', '하한가', '종목', '특징주', '거래량', '매수',
    '매도', '실적', '영업이익', '순이익', '흑자', '적자', '신고가', '신저가', '목표가',
    '수주', '공시', '배당', '증자', '감자', '합병', '인수', '테마', '코스닥', '코스피',
    '외국인', '순매수', '순매도', '상장', '시가총액', '시총', 'VI', '어닝', '턴어라운드',
    '공매도', '배당락', '액면분할', '무상증자', '주식',
)

# 한국 주식 헤드라인 감성 사전 (부분 문자열 매칭)
POS_WORDS = (
    '상한가', '급등', '신고가', '최고가', '사상최대', '역대', '돌파', '강세', '상승',
    '반등', '순매수', '매수세', '흑자', '호실적', '어닝서프라이즈', '실적개선', '개선',
    '성장', '수주', '수혜', '호재', '기대', '목표가 상향', '목표주가 상향', '상향',
    '승인', '허가', '계약', '공급', '협약', '흥행', '성공', '회복', '턴어라운드',
    '무상증자', '자사주', '배당확대', '최대 실적', '깜짝 실적', '수익성 개선',
)
NEG_WORDS = (
    '하한가', '급락', '신저가', '최저가', '약세', '하락', '이탈', '순매도', '매도세',
    '적자', '부진', '어닝쇼크', '실적악화', '악화', '우려', '리스크', '경고', '소송',
    '제재', '과징금', '횡령', '배임', '유상증자', '감자', '상장폐지', '관리종목',
    '조사', '압수수색', '부도', '파산', '결손', '목표가 하향', '목표주가 하향', '하향',
    '손실', '불성실', '거래정지', '구조조정', '철수', '매각', '최대 주주 변경',
)


def log(msg):
    print(msg, flush=True)


def _read_universe(limit):
    """스크리너 JSON 들에서 (ticker, name) 유니버스를 구성한다."""
    seen = {}
    order = []
    for fn in SOURCE_FILES:
        path = os.path.join(DATA_DIR, fn)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding='utf-8') as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for item in payload.get('data', []) or []:
            tk = str(item.get('ticker', '')).strip()
            name = str(item.get('name', '')).strip()
            if tk and tk not in seen and name:
                seen[tk] = name
                order.append((tk, name))
    if limit:
        order = order[:limit]
    return order


def _fetch(query, retries=3):
    params = {'q': query, 'hl': 'ko', 'gl': 'KR', 'ceid': 'KR:ko'}
    last = None
    for i in range(retries):
        try:
            res = requests.get(RSS_ENDPOINT, params=params, timeout=30, headers=HEADERS)
            if res.status_code == 200:
                return res.content
            last = 'HTTP %d' % res.status_code
        except requests.RequestException as exc:
            last = repr(exc)
        time.sleep(1.5 * (i + 1))
    log('  ! RSS 실패 (%s): %s' % (query, last))
    return None


def _parse(content):
    root = ET.fromstring(content)
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = []
    for it in root.findall('.//item'):
        pub = it.findtext('pubDate')
        dt = None
        if pub:
            try:
                dt = email.utils.parsedate_to_datetime(pub)
                if dt and dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
            except (TypeError, ValueError):
                dt = None
        rows.append({
            'title': (it.findtext('title') or '').strip(),
            'source': (it.findtext('source') or '').strip(),
            'link': (it.findtext('link') or '').strip(),
            'date': dt.isoformat() if dt else None,
            '_age': (now - dt).days if dt else None,
        })
    return rows


def _has_stock_hint(title):
    upper = title.upper()
    return any(h.upper() in upper for h in STOCK_HINTS)


def collect(ticker, name, use_name=True):
    """한 종목의 뉴스 기사를 수집해 중복 제거한다."""
    merged = {}
    queries = [ticker]
    if use_name and name:
        queries.append(name)

    for q in queries:
        content = _fetch(q)
        time.sleep(REQ_DELAY)
        if not content:
            continue
        for row in _parse(content):
            title = row['title']
            if not title:
                continue
            # 이름 검색은 이름 + 주가 키워드가 함께 있는 기사만 채택 (동음이의 노이즈 제거)
            if use_name and q == name and not (name in title and _has_stock_hint(title)):
                continue
            merged.setdefault(title, row)
    return list(merged.values())


def _sentiment(titles):
    pos = neg = 0
    for t in titles:
        pos += sum(1 for w in POS_WORDS if w in t)
        neg += sum(1 for w in NEG_WORDS if w in t)
    if pos == 0 and neg == 0:
        return 'neutral', pos, neg
    if pos > neg * 1.2:
        return 'positive', pos, neg
    if neg > pos * 1.2:
        return 'negative', pos, neg
    return 'neutral', pos, neg


def build_item(ticker, name, use_name=True):
    rows = collect(ticker, name, use_name=use_name)
    rows.sort(key=lambda r: r['date'] or '', reverse=True)

    recent = lambda n: sum(1 for r in rows if r['_age'] is not None and r['_age'] <= n)
    src = {}
    for r in rows:
        if r['source']:
            src[r['source']] = src.get(r['source'], 0) + 1
    top_sources = sorted(src.items(), key=lambda kv: -kv[1])[:8]

    sentiment, pos, neg = _sentiment([r['title'] for r in rows])
    samples = [
        {'title': r['title'], 'source': r['source'], 'date': r['date'], 'link': r['link']}
        for r in rows[:SAMPLE_LIMIT]
    ]
    return {
        'ticker': ticker,
        'name': name,
        'total_mentions': len(rows),
        'recent_7d': recent(7),
        'recent_30d': recent(30),
        'sentiment': sentiment,
        'positive_hits': pos,
        'negative_hits': neg,
        'top_sources': [[s, c] for s, c in top_sources],
        'samples': samples,
    }


def main():
    parser = argparse.ArgumentParser(description='Google News RSS 뉴스 언급량 수집기')
    parser.add_argument('tickers', nargs='*', help='특정 종목코드 (미지정 시 스크리너 결과에서 자동)')
    parser.add_argument('--limit', type=int, default=MAX_STOCKS, help='최대 종목 수')
    parser.add_argument('--no-name', action='store_true', help='이름 검색 생략 (코드 검색만)')
    args = parser.parse_args()

    if args.tickers:
        universe = [(t, t) for t in args.tickers]  # 이름 미상 → 코드만 검색
        use_name = False
    else:
        universe = _read_universe(args.limit)
        use_name = not args.no_name

    if not universe:
        log('유니버스가 비어 있습니다. 스크리너를 먼저 실행하세요.')
        return 1

    log('뉴스 수집 대상: %d개 종목' % len(universe))
    items = []
    for i, (ticker, name) in enumerate(universe, 1):
        item = build_item(ticker, name, use_name=use_name)
        items.append(item)
        log('  [%d/%d] %s %s → %d건 (%s, 7일 %d)'
            % (i, len(universe), ticker, name, item['total_mentions'],
               item['sentiment'], item['recent_7d']))

    payload = {
        'meta': {
            'lastUpdated': datetime.datetime.now().isoformat(),
            'source': 'google_news_rss',
            'totalCount': len(items),
        },
        'data': items,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log('저장 완료: %s (%d개)' % (OUTPUT_FILE, len(items)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
