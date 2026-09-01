#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Japan Made屋 年計 (移動年計 rolling 12M) データ生成スクリプト

BASE 5 CSV (2021-05〜2026-04) + Shopify Admin GraphQL orders (2026-02〜先月末)
を月次で合算し、移動年計 (直近12ヶ月合計) を計算して docs/jm-yearly.json を出力。

対話モード (先月まで) 実行:
  set -a; source ~/JM自動化/customer-master/.env; set +a
  python3 scripts/generate_yearly.py

出力 JSON 構造:
  {
    "shop": "Japan Made屋",
    "updatedAt": "2026-09-01T...",
    "definition": "移動年計 (rolling 12M) = 各月末の直近12ヶ月合計",
    "months": [
      {"month": "2021-05", "monthly_sales": 32600, "rolling12m": null, "rolling12m_prev": null},
      ...
      {"month": "2026-08", "monthly_sales": 24513543, "rolling12m": 210000000, "rolling12m_prev": 180000000}
    ],
    "latest": {"month": "2026-08", "rolling12m": ..., "yoy_pct": 16.7}
  }
"""
import os
import sys
import csv
import io
import json
import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
import urllib.request
import urllib.parse

JST = ZoneInfo("Asia/Tokyo")
SHOP_NAME = "Japan Made屋"
API_VERSION = "2026-01"

BASE_CSV_FILES = [
    f"/Users/takuma/Documents/社ロゴ/顧客データ/{y}.csv"
    for y in [2021, 2022, 2023, 2024, 2025]
]
OUT_PATH = os.environ.get("OUT_PATH", "docs/jm-yearly.json")


def aggregate_base_monthly():
    """BASE 5 CSV → 月次売上 dict {YYYY-MM: sales_int}"""
    monthly = defaultdict(int)
    for f in BASE_CSV_FILES:
        with open(f, "rb") as fh:
            text = fh.read().decode("shift_jis", errors="replace")
        reader = csv.reader(io.StringIO(text))
        next(reader)  # skip header
        for row in reader:
            if len(row) < 24:
                continue
            try:
                dt = datetime.datetime.strptime(row[1].strip('"'), "%Y-%m-%d %H:%M:%S")
                amount = int(float(row[23]))
            except (ValueError, IndexError):
                continue
            ym = f"{dt.year:04d}-{dt.month:02d}"
            monthly[ym] += amount
    return dict(monthly)


def fetch_shopify_token():
    store = os.environ["SHOPIFY_STORE"].strip()
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": os.environ["SHOPIFY_CLIENT_ID"].strip(),
        "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"].strip(),
    }).encode()
    req = urllib.request.Request(
        f"https://{store}/admin/oauth/access_token",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]


def aggregate_shopify_monthly(start_ym: str, end_ym: str):
    """Shopify orders → 月次売上 dict {YYYY-MM: sales_int}

    start_ym / end_ym は YYYY-MM 形式 (両端含む)。
    currentTotalPriceSet (返金反映後・税込) を SUM。
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
            nodes{ createdAt currentTotalPriceSet{shopMoney{amount}} }
          }
        }
        """
        data = gql(q, {"n": 250, "after": cursor, "q": query_str})["orders"]
        for n in data["nodes"]:
            ym = n["createdAt"][0:7]
            amt = float(n["currentTotalPriceSet"]["shopMoney"]["amount"])
            monthly[ym] += int(round(amt))
        print(f"  [Shopify page {page}] {len(data['nodes'])} orders", file=sys.stderr)
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]

    return dict(monthly)


def month_range(start_ym: str, end_ym: str):
    """YYYY-MM 文字列を start→end で列挙"""
    sy, sm = map(int, start_ym.split("-"))
    ey, em = map(int, end_ym.split("-"))
    result = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y += 1
            m = 1
    return result


def calc_rolling_12m(months_list, monthly_sales_dict):
    """各月の rolling 12M (直近12ヶ月合計・当該月含む) を計算。
    12ヶ月分揃わない先頭11ヶ月は None。
    """
    rolling = {}
    for i, ym in enumerate(months_list):
        if i < 11:
            rolling[ym] = None
        else:
            window = months_list[i - 11 : i + 1]
            rolling[ym] = sum(monthly_sales_dict.get(m, 0) for m in window)
    return rolling


def prev_year_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{y - 1:04d}-{m:02d}"


def main():
    now = datetime.datetime.now(JST)
    today = now.date()

    # 対話モード = 先月末まで (今月の未完月は除外)
    if today.month == 1:
        end_ym = f"{today.year - 1:04d}-12"
    else:
        end_ym = f"{today.year:04d}-{today.month - 1:02d}"

    # BASE 全期間 + Shopify 開始月推定
    print("[1/3] BASE 集計中...", file=sys.stderr)
    base_monthly = aggregate_base_monthly()
    print(f"  BASE: {len(base_monthly)} months (¥{sum(base_monthly.values()):,})",
          file=sys.stderr)

    print(f"[2/3] Shopify 集計中 (2026-02〜{end_ym})...", file=sys.stderr)
    shopify_monthly = aggregate_shopify_monthly("2026-02", end_ym)
    print(f"  Shopify: {len(shopify_monthly)} months (¥{sum(shopify_monthly.values()):,})",
          file=sys.stderr)

    # 合算
    print("[3/3] 合算 + rolling 12M 計算...", file=sys.stderr)
    all_months = set(base_monthly) | set(shopify_monthly)
    start_ym = min(all_months)  # "2021-05"
    months_list = month_range(start_ym, end_ym)

    monthly_sales = {}
    for ym in months_list:
        monthly_sales[ym] = base_monthly.get(ym, 0) + shopify_monthly.get(ym, 0)

    rolling = calc_rolling_12m(months_list, monthly_sales)

    # 前年同月の rolling を紐付け
    months_out = []
    for ym in months_list:
        r_current = rolling[ym]
        r_prev = rolling.get(prev_year_month(ym))
        months_out.append({
            "month": ym,
            "monthly_sales": monthly_sales[ym],
            "rolling12m": r_current,
            "rolling12m_prev": r_prev,
        })

    # 最新月の YoY%
    latest = months_out[-1]
    if latest["rolling12m"] and latest["rolling12m_prev"]:
        yoy_pct = round(
            (latest["rolling12m"] - latest["rolling12m_prev"]) / latest["rolling12m_prev"] * 100,
            1,
        )
    else:
        yoy_pct = None

    out = {
        "shop": SHOP_NAME,
        "updatedAt": now.replace(microsecond=0).isoformat(),
        "definition": "移動年計 (rolling 12M) = 当該月末の直近12ヶ月合計売上",
        "months": months_out,
        "latest": {
            "month": latest["month"],
            "rolling12m": latest["rolling12m"],
            "rolling12m_prev": latest["rolling12m_prev"],
            "yoy_pct": yoy_pct,
        },
    }

    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"\n✓ wrote {OUT_PATH}", file=sys.stderr)
    print(f"  期間: {months_list[0]} 〜 {months_list[-1]} ({len(months_list)}ヶ月)", file=sys.stderr)
    print(f"  最新 rolling12m: ¥{latest['rolling12m']:,}", file=sys.stderr)
    if yoy_pct is not None:
        print(f"  前年同月比: {yoy_pct:+.1f}%", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
