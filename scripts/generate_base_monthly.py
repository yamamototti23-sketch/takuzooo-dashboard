#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BASE 5 CSV → 月次集計 JSON 事前生成 (個人情報を含まない集計値のみ)

対話モードで 1回だけ実行し、docs/base_monthly.json を生成する。
生成後は public repo に commit 可能 (顧客名/住所/電話/メール は含まれない)。
"""
import csv
import io
import json
import datetime
from collections import defaultdict
from pathlib import Path

BASE_CSV_FILES = [
    Path(f"/Users/takuma/Documents/社ロゴ/顧客データ/{y}.csv")
    for y in [2021, 2022, 2023, 2024, 2025]
]
OUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "base_monthly.json"


def main():
    monthly = defaultdict(int)
    order_ids_by_month = defaultdict(set)
    for f in BASE_CSV_FILES:
        if not f.exists():
            print(f"[SKIP] {f} not found")
            continue
        text = f.read_bytes().decode("shift_jis", errors="replace")
        reader = csv.reader(io.StringIO(text))
        next(reader)
        for row in reader:
            if len(row) < 24:
                continue
            try:
                order_id = row[0]
                dt = datetime.datetime.strptime(row[1].strip('"'), "%Y-%m-%d %H:%M:%S")
                amount = int(float(row[23]))
            except (ValueError, IndexError):
                continue
            ym = f"{dt.year:04d}-{dt.month:02d}"
            monthly[ym] += amount
            order_ids_by_month[ym].add(order_id)

    months = []
    for ym in sorted(monthly.keys()):
        months.append({
            "month": ym,
            "sales": monthly[ym],
            "orders": len(order_ids_by_month[ym]),
        })

    payload = {
        "source": "BASE",
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "note": "BASE 5 CSV (2021-05〜2026-04) を月次集計した値のみ (個人情報なし)。2026-04 で BASE 終了確定・以後追加なし。",
        "months": months,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total_sales = sum(m["sales"] for m in months)
    total_orders = sum(m["orders"] for m in months)
    print(f"✓ wrote {OUT_PATH}")
    print(f"  月数: {len(months)} / 累計売上: ¥{total_sales:,} / 累計注文: {total_orders}件")


if __name__ == "__main__":
    main()
