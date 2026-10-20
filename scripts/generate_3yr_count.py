#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Japan Made屋 月間販売個数 (注文件数) 3年比較 データ生成スクリプト (kiosk 5枚目用)

BASE 月次 (base_monthly.json の orders field・2021-05〜2026-04 事前集計) +
Shopify Admin GraphQL orders 件数 (2026-02〜当月) を合算し、直近3年の月次
注文件数を出力。

「販売個数」= 注文件数 (何回注文があったか・件数カウント)。
商品数量合計 (lineItems.quantity 合計) ではないことに注意。
BASE 時代の orders field との連続性担保のため注文件数ベース採用 (2026-10-20 たくぞう明示)。

対話モード (当月まで) 実行:
  set -a; source ~/JM自動化/customer-master/.env; set +a
  python3 scripts/generate_3yr_count.py

出力 JSON (docs/jm-3yr-count.json):
  {
    "shop": "Japan Made屋",
    "updatedAt": "2026-10-20T...",
    "metric": "orders",
    "unit": "count",
    "note": "BASE+Shopify 合算・月次注文件数・直近3年ローリング",
    "years": [2024, 2025, 2026],
    "current_year": 2026, "current_month": 10,
    "data": {"2024": {"1": 423, ...}, ...}
  }
"""
import os
import sys
import json
import datetime
import urllib.request
import urllib.parse
from collections import defaultdict
from zoneinfo import ZoneInfo

# 既存の BASE 集計関数 + token 取得は generate_yearly.py から流用
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from generate_yearly import (  # type: ignore
    aggregate_base_monthly,
    fetch_shopify_token,
)

JST = ZoneInfo("Asia/Tokyo")
SHOP_NAME = "Japan Made屋"
API_VERSION = "2026-01"
OUT_PATH = os.environ.get("OUT_PATH_3YR_COUNT", "docs/jm-3yr-count.json")


def aggregate_base_monthly_orders():
    """BASE 月次注文件数 dict {YYYY-MM: orders_count} を返す。
    base_monthly.json の orders field を参照 (sales ではなく件数)。
    """
    BASE_MONTHLY_JSON = os.environ.get("BASE_MONTHLY_JSON", "docs/base_monthly.json")
    if os.path.exists(BASE_MONTHLY_JSON):
        with open(BASE_MONTHLY_JSON, encoding="utf-8") as f:
            payload = json.load(f)
        monthly = {m["month"]: int(m["orders"]) for m in payload.get("months", [])}
        print(f"[BASE] loaded {len(monthly)} months orders from {BASE_MONTHLY_JSON}",
              file=sys.stderr)
        return monthly
    raise RuntimeError(f"BASE_MONTHLY_JSON not found: {BASE_MONTHLY_JSON}")


def aggregate_shopify_monthly_orders(start_ym: str, end_ym: str):
    """Shopify orders → 月次注文件数 dict {YYYY-MM: count}
    金額は見ず nodes の件数を月別に集計。
    """
    store = os.environ["SHOPIFY_STORE"].strip()
    token = fetch_shopify_token()
    endpoint = f"https://{store}/admin/api/{API_VERSION}/graphql.json"

    def gql(query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(endpoint, data=body, headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        })
        r = json.loads(urllib.request.urlopen(req, timeout=60).read())
        if "errors" in r and r["errors"]:
            raise RuntimeError(f"GraphQL error: {r['errors']}")
        return r["data"]

    # end_ym の月末日を算出
    end_year, end_month = map(int, end_ym.split("-"))
    if end_month == 12:
        end_last_day = 31
    else:
        first_next = datetime.date(end_year, end_month + 1, 1)
        end_last_day = (first_next - datetime.timedelta(days=1)).day

    query_str = (
        f"created_at:>={start_ym}-01 "
        f"created_at:<={end_ym}-{end_last_day:02d} "
        "AND financial_status:paid,partially_paid,partially_refunded,refunded"
    )

    monthly = defaultdict(int)
    cursor = None
    page = 0
    while True:
        page += 1
        q = """
        query($n:Int!,$after:String,$q:String!){
          orders(first:$n, after:$after, query:$q) {
            pageInfo{hasNextPage endCursor}
            nodes{ createdAt }
          }
        }
        """
        data = gql(q, {"n": 250, "after": cursor, "q": query_str})["orders"]
        for n in data["nodes"]:
            ym = n["createdAt"][0:7]
            monthly[ym] += 1  # 件数カウント
        print(f"  [Shopify page {page}] {len(data['nodes'])} orders", file=sys.stderr)
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]

    return dict(monthly)


def main():
    now = datetime.datetime.now(JST)
    today = now.date()
    current_year = today.year
    current_month = today.month

    years = [current_year - 2, current_year - 1, current_year]
    end_ym = f"{current_year:04d}-{current_month:02d}"

    print(f"[1/3] BASE 件数 集計中...", file=sys.stderr)
    base_monthly = aggregate_base_monthly_orders()
    print(f"  BASE: {len(base_monthly)} months loaded", file=sys.stderr)

    print(f"[2/3] Shopify 件数 集計中 (2026-02〜{end_ym})...", file=sys.stderr)
    shopify_start = "2026-02"
    if shopify_start > end_ym:
        shopify_monthly = {}
        print(f"  Shopify: skip (期間外)", file=sys.stderr)
    else:
        shopify_monthly = aggregate_shopify_monthly_orders(shopify_start, end_ym)
        print(f"  Shopify: {len(shopify_monthly)} months ({sum(shopify_monthly.values()):,} orders)",
              file=sys.stderr)

    print(f"[3/3] 合算 + 3年dict 整形中...", file=sys.stderr)
    data = {}
    for y in years:
        year_dict = {}
        for m in range(1, 13):
            ym = f"{y:04d}-{m:02d}"
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
        "metric": "orders",
        "unit": "count",
        "note": "BASE+Shopify 合算・月次注文件数 (件数カウント・lineItems数量ではない)・直近3年ローリング",
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
    print(f"  years: {years}  current: {current_year}-{current_month:02d}", file=sys.stderr)
    for y in years:
        total = sum(v for v in data[str(y)].values() if v)
        months_filled = sum(1 for v in data[str(y)].values() if v)
        print(f"  {y}: {months_filled}ヶ月 {total:,} 件", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
