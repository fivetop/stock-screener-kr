#!/usr/bin/env python3
"""
'오늘 새로 걸린 종목'(신규 판정)을 MongoDB에 저장한다.

프론트(주식_스크리너_전체.html 의 renderFreshStocks)와 동일한 규칙으로
data/*.json 에서 신규 종목을 산출해 MongoDB 컬렉션에 upsert 한다.

규칙 (renderFreshStocks 와 동일):
  - 52주 신고가: days_since == 0
  - 박스권 돌파(거래량/단순): days_since_breakout == 0
  - 거래량 폭발: change_rate 상위 3개
  - ticker 중복 제거 후 상위 10개

환경변수:
  MONGO_URI          MongoDB 연결 문자열 (예: mongodb+srv://user:pass@host/)
  MONGO_DB           데이터베이스 이름 (기본: stock)
  MONGO_COLLECTION   컬렉션 이름 (기본: fresh_stocks)

MONGO_URI 가 없으면 저장을 건너뛰고 경고만 출력한다(CI 안전).
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(HERE, "data")
DEFAULT_DB = os.environ.get("MONGO_DB") or "stock"
DEFAULT_COLLECTION = os.environ.get("MONGO_COLLECTION") or "fresh_stocks"
TOP_VOLUME = 3
MAX_ITEMS = 10


def _rows(data_dir: str, name: str) -> list:
    path = os.path.join(data_dir, name)
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        print(f"::warning:: {name} 없음 — 건너뜀")
        return []
    except Exception as exc:  # noqa: BLE001
        print(f"::warning:: {name} 로드 실패: {exc}")
        return []
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    return []


def build_fresh_stocks(data_dir: str = DEFAULT_DATA_DIR) -> list:
    """renderFreshStocks() 와 동일한 신규 종목 리스트를 만든다."""
    items = []

    for s in _rows(data_dir, "new_high_52w.json"):
        if s.get("days_since") == 0:
            items.append({"src": "52주 신고가", "s": s, "resistance": s.get("high_52w")})

    for fname in ("box_breakout.json", "box_breakout_simple.json"):
        for s in _rows(data_dir, fname):
            if s.get("days_since_breakout") == 0:
                items.append({"src": "박스권 돌파", "s": s, "resistance": None})

    volume = sorted(
        _rows(data_dir, "volume_explosion.json"),
        key=lambda x: x.get("change_rate") or 0,
        reverse=True,
    )[:TOP_VOLUME]
    for s in volume:
        items.append({"src": "거래량 폭발", "s": s, "resistance": None})

    seen = set()
    uniq = []
    for it in items:
        ticker = it["s"].get("ticker")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        uniq.append(it)
    return uniq[:MAX_ITEMS]


def to_docs(uniq: list, date_str: str, captured_at: str) -> list:
    docs = []
    for it in uniq:
        s = it["s"]
        docs.append({
            "date": date_str,
            "ticker": str(s.get("ticker")),
            "name": s.get("name"),
            "price": s.get("price"),
            "change_rate": s.get("change_rate"),
            "source": it["src"],
            "resistance": it.get("resistance"),
            "market_cap": s.get("market_cap"),
            "captured_at": captured_at,
        })
    return docs


def save(docs: list, uri: str, db_name: str, coll_name: str):
    from pymongo import MongoClient, UpdateOne

    client = MongoClient(uri, serverSelectionTimeoutMS=20000)
    coll = client[db_name][coll_name]
    coll.create_index([("date", 1), ("ticker", 1)], unique=True)

    ops = []
    for d in docs:
        fields = {k: v for k, v in d.items() if k not in ("date", "ticker")}
        ops.append(UpdateOne(
            {"date": d["date"], "ticker": d["ticker"]},
            {"$set": fields, "$setOnInsert": {"first_seen_at": d["captured_at"]}},
            upsert=True,
        ))
    if not ops:
        return None
    return coll.bulk_write(ops, ordered=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="오늘 새로 걸린 종목을 MongoDB에 저장")
    ap.add_argument("--date", help="저장 날짜(YYYY-MM-DD, 기본: 오늘 KST)")
    ap.add_argument("--dry-run", action="store_true", help="MongoDB 저장 없이 결과만 출력")
    ap.add_argument("--uri", help="MongoDB URI (기본: 환경변수 MONGO_URI)")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"데이터베이스 이름 (기본: {DEFAULT_DB})")
    ap.add_argument("--collection", default=DEFAULT_COLLECTION,
                    help=f"컬렉션 이름 (기본: {DEFAULT_COLLECTION})")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="data 디렉터리 경로")
    args = ap.parse_args()

    now = datetime.now(KST)
    date_str = args.date or now.strftime("%Y-%m-%d")
    captured_at = now.isoformat(timespec="seconds")

    uniq = build_fresh_stocks(args.data_dir)
    docs = to_docs(uniq, date_str, captured_at)
    print(f"[fresh_stocks] {date_str} 신규 종목 {len(docs)}개")
    for d in docs:
        print(f"  - {d['ticker']} {d['name']} ({d['source']}, {d['change_rate']})")

    if args.dry_run:
        print("(dry-run) MongoDB 저장 생략")
        return 0

    uri = args.uri or os.environ.get("MONGO_URI")
    if not uri:
        print("::warning:: MONGO_URI 가 없어 MongoDB 저장을 건너뜁니다.")
        return 0

    try:
        res = save(docs, uri, args.db, args.collection)
    except Exception as exc:  # noqa: BLE001
        print(f"::error:: MongoDB 저장 실패: {exc}")
        return 1

    upserted = getattr(res, "upserted_count", 0) if res else 0
    modified = getattr(res, "modified_count", 0) if res else 0
    print(f"저장 완료 → {args.db}.{args.collection} (upsert {upserted}, modified {modified})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
