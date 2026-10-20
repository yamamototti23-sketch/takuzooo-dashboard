#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Japan Made屋 月間売上金額 3年比較 データ生成スクリプト (kiosk 4枚目用)

BASE 月次 (base_monthly.json・2021-05〜2026-04 事前集計) + Shopify Admin GraphQL
orders (2026-02〜当月) を合算し、直近3年 (当年+過去2年) の月次売上を出力。

対話モード (当月まで) 実行:
  set -a; source ~/JM自動化/customer-master/.env; set +a
  python3 scripts/generate_3yr_amount.py

出力 JSON (docs/jm-3yr-amount.json):
  {
    "shop": "Japan Made屋",
    "updatedAt": "2026-10-20T...",
    "metric": "sales",
    "unit": "yen",
    "note": "BASE+Shopify 合算・月次売上・直近3年ローリング (案a 1/1即drop)",
    "years": [2024, 2025, 2026],
    "current_year": 2026,
    "current_month": 10,
    "data": {
      "2024": {"1": 7905950, "2": 5509620, ..., "12": 24923210},
      "2025": {"1": 13107730, ..., "12": 33820450},
      "2026": {"1": 18290000, ..., "10": null}
    }
  }
"""
import os
import sys
import json
import datetime
from zoneinfo import ZoneInfo

# generate_yearly.py の BASE / Shopify 集計関数を流用
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from generate_yearly import (  # type: ignore
    aggregate_base_monthly,
    aggregate_shopify_monthly,
)

JST = ZoneInfo("Asia/Tokyo")
SHOP_NAME = "Japan Made屋"
OUT_PATH = os.environ.get("OUT_PATH_3YR", "docs/jm-3yr-amount.json")


def main():
    now = datetime.datetime.now(JST)
    today = now.date()
    current_year = today.year
    current_month = today.month

    # 3年ローリング (案a: 1/1 即drop) = 当年 + 過去2年
    years = [current_year - 2, current_year - 1, current_year]
    start_ym = f"{years[0]:04d}-01"
    end_ym = f"{current_year:04d}-{current_month:02d}"

    print(f"[1/3] BASE 集計中...", file=sys.stderr)
    base_monthly = aggregate_base_monthly()
    print(f"  BASE: {len(base_monthly)} months loaded", file=sys.stderr)

    # Shopify は 2026-02 以降存在 (BASE終了は 2026-04)
    print(f"[2/3] Shopify 集計中 (2026-02〜{end_ym})...", file=sys.stderr)
    shopify_start = "2026-02"
    if shopify_start > end_ym:
        shopify_monthly = {}
        print(f"  Shopify: skip (期間外)", file=sys.stderr)
    else:
        shopify_monthly = aggregate_shopify_monthly(shopify_start, end_ym)
        print(f"  Shopify: {len(shopify_monthly)} months (¥{sum(shopify_monthly.values()):,})",
              file=sys.stderr)

    # 合算 + 3年×12ヶ月 dict に整形
    print(f"[3/3] 合算 + 3年dict 整形中...", file=sys.stderr)
    data = {}
    for y in years:
        year_dict = {}
        for m in range(1, 13):
            ym = f"{y:04d}-{m:02d}"
            # 当月より未来 = None (折れ線を途切れさせる)
            if (y, m) > (current_year, current_month):
                year_dict[str(m)] = None
                continue
            base_val = base_monthly.get(ym, 0)
            shopify_val = shopify_monthly.get(ym, 0)
            total = base_val + shopify_val
            year_dict[str(m)] = total if total > 0 else None
        data[str(y)] = year_dict

    out = {
        "shop": SHOP_NAME,
        "updatedAt": now.replace(microsecond=0).isoformat(),
        "metric": "sales",
        "unit": "yen",
        "note": "BASE+Shopify 合算・月次売上・直近3年ローリング (1/1 即drop)",
        "years": years,
        "current_year": current_year,
        "current_month": current_month,
        "data": data,
    }

    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"\n✓ wrote {OUT_PATH}", file=sys.stderr)
    print(f"  years: {years}", file=sys.stderr)
    print(f"  current: {current_year}-{current_month:02d}", file=sys.stderr)
    for y in years:
        total = sum(v for v in data[str(y)].values() if v)
        months_filled = sum(1 for v in data[str(y)].values() if v)
        print(f"  {y}: {months_filled}ヶ月 ¥{total:,}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
