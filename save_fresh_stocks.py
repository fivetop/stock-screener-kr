#!/usr/bin/env python3
"""
'오늘 새로 걸린 종목'(신규 판정)을 data/fresh_history.json 에 누적 저장한다.

프론트(주식_스크리너_전체.html 의 renderFreshStocks)와 동일한 규칙으로
data/*.json 에서 신규 종목을 산출해 날짜별로 누적한다. (MongoDB 불필요)

규칙 (renderFreshStocks 와 동일):
  - 52주 신고가: days_since == 0
  - 박스권 돌파(거래량/단순): days_since_breakout == 0
  - 거래량 폭발: change_rate 상위 3개
  - ticker 중복 제거 후 상위 10개

기준일은 meta.marketDate(데이터 기준일)를 우선 사용하고, 없으면 오늘(KST)을 쓴다.
같은 날짜에 다시 실행하면 그 날 항목을 현재 결과로 교체한다(멱등).
결과가 이전과 같으면 파일을 다시 쓰지 않아 불필요한 커밋을 만들지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(HERE, "data")
HISTORY_FILE = "fresh_history.json"
TOP_VOLUME = 3
MAX_ITEMS = 10
DEFAULT_KEEP_DAYS = 730


def _load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _rows(data_dir: str, name: str) -> list:
    path = os.path.join(data_dir, name)
    try:
        payload = _load_json(path)
    except FileNotFoundError:
        print(f"::warning:: {name} 없음 — 건너뜀")
        return []
    except Exception as exc:  # noqa: BLE001
        print(f"::warning:: {name} 로드 실패: {exc}")
        return []
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return []


def _market_date(data_dir: str):
    """data/*.json 의 meta.marketDate 중 가장 최신값."""
    dates = []
    for name in ("new_high_52w.json", "box_breakout.json",
                 "box_breakout_simple.json", "volume_explosion.json"):
        try:
            meta = _load_json(os.path.join(data_dir, name)).get("meta") or {}
        except Exception:  # noqa: BLE001
            continue
        d = meta.get("marketDate")
        if d:
            dates.append(str(d))
    return max(dates) if dates else None


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


def to_records(uniq: list, date_str: str, captured_at: str) -> list:
    records = []
    for it in uniq:
        s = it["s"]
        records.append({
            "date": date_str,
            "ticker": str(s.get("ticker")),
            "name": s.get("name"),
            "price": s.get("price"),
            "change_rate": s.get("change_rate"),
            "source": it["src"],
            "resistance": it.get("resistance"),
            "market_cap": s.get("market_cap"),
            "first_seen_at": captured_at,
            "last_seen_at": captured_at,
        })
    return records


def _load_history(path: str) -> list:
    try:
        obj = _load_json(path)
        if isinstance(obj, dict) and isinstance(obj.get("data"), list):
            return obj["data"]
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        print(f"::warning:: {path} 로드 실패({exc}) — 새로 작성")
    return []


def _signature(records: list) -> list:
    """last_seen_at/meta 를 제외한 비교용 서명 (변경 감지)."""
    keys = ("date", "ticker", "name", "price", "change_rate", "source", "resistance", "first_seen_at")
    return [tuple(r.get(k) for k in keys) for r in records]


def _prune(records: list, keep_days: int) -> list:
    if keep_days <= 0:
        return records
    dates = sorted({r.get("date") for r in records if r.get("date")}, reverse=True)
    keep = set(dates[:keep_days])
    return [r for r in records if r.get("date") in keep]


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="오늘 새로 걸린 종목을 data/fresh_history.json 에 누적")
    ap.add_argument("--date", help="기준일(YYYY-MM-DD, 기본: meta.marketDate → 오늘 KST)")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="data 디렉터리 경로")
    ap.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS, help=f"보존 일수(기본 {DEFAULT_KEEP_DAYS})")
    ap.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 결과만 출력")
    args = ap.parse_args()

    now = datetime.now(KST)
    captured_at = now.isoformat(timespec="seconds")
    date_str = args.date or _market_date(args.data_dir) or now.strftime("%Y-%m-%d")

    uniq = build_fresh_stocks(args.data_dir)
    new_records = to_records(uniq, date_str, captured_at)
    print(f"[fresh_history] 기준일 {date_str} 신규 종목 {len(new_records)}개")

    path = os.path.join(args.data_dir, HISTORY_FILE)
    existing = _load_history(path)

    # 같은 (date, ticker)가 이미 있으면 first_seen_at 유지
    prev_first = {(r.get("date"), r.get("ticker")): r.get("first_seen_at") for r in existing}
    for r in new_records:
        r["first_seen_at"] = prev_first.get((r["date"], r["ticker"])) or r["last_seen_at"]

    others = [r for r in existing if r.get("date") != date_str]
    merged = _prune(others + new_records, args.keep_days)

    if _signature(merged) == _signature(existing):
        print("변경 없음 — fresh_history.json 그대로 유지")
        for r in new_records:
            print(f"  - {r['ticker']} {r['name']} ({r['source']}, {r['change_rate']})")
        return 0

    for r in new_records:
        print(f"  - {r['ticker']} {r['name']} ({r['source']}, {r['change_rate']})")

    if args.dry_run:
        print("(dry-run) 파일 저장 생략")
        return 0

    dates = {r.get("date") for r in merged if r.get("date")}
    payload = {
        "meta": {
            "lastUpdated": captured_at,
            "source": "fresh_history",
            "totalCount": len(merged),
            "dateCount": len(dates),
            "latestDate": max(dates) if dates else None,
        },
        "data": merged,
    }
    _write_json(path, payload)
    print(f"저장 완료 → {path} (총 {len(merged)}건, {len(dates)}일)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
