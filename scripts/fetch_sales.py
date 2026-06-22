#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Japan Made屋 売上ダッシュボード用 jm-data.json 生成スクリプト

Shopify Admin GraphQL の ShopifyQL（shopifyqlQuery）を使い、Shopify Analytics と
「完全に同じ数字」で当日の売上指標を取得し docs/jm-data.json を出力する。
GitHub Actions から15分ごとに実行する想定（PC不要）。

必要な環境変数:
  SHOPIFY_STORE          例: 9a6736-3.myshopify.com
  SHOPIFY_CLIENT_ID      Dev Dashboard アプリの Client ID
  SHOPIFY_CLIENT_SECRET  同 Client Secret
                         起動時に client_credentials grant で access_token を取得し、
                         以降の Admin GraphQL 呼び出しに X-Shopify-Access-Token として使う。
                         必要スコープ: read_products, read_orders, read_reports
任意:
  OUT_PATH        出力先（既定: docs/jm-data.json）

出力フォーマットは docs/jm-dashboard.html の SAMPLE_DATA と同一構造。
"""

import os
import sys
import json
import datetime
from zoneinfo import ZoneInfo
import urllib.request
import urllib.parse

API_VERSION = "2026-01"          # publicApiVersions で supported を確認済み（必要なら更新）
JST = ZoneInfo("Asia/Tokyo")     # Shopify ストアのタイムゾーンに合わせる
SHOP_NAME = "Japan Made屋"
REFRESH_MIN = 30
OUT_PATH = os.environ.get("OUT_PATH", "docs/jm-data.json")

STORE = os.environ["SHOPIFY_STORE"].strip()
CLIENT_ID = os.environ["SHOPIFY_CLIENT_ID"].strip()
CLIENT_SECRET = os.environ["SHOPIFY_CLIENT_SECRET"].strip()
ENDPOINT = f"https://{STORE}/admin/api/{API_VERSION}/graphql.json"
OAUTH_ENDPOINT = f"https://{STORE}/admin/oauth/access_token"

# main() 冒頭で client_credentials grant により取得して上書きする
TOKEN = None


def fetch_access_token():
    """Dev Dashboard アプリの client_id / client_secret から
    access_token を取得する (OAuth 2.0 client_credentials grant)。"""
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode("utf-8")
    req = urllib.request.Request(OAUTH_ENDPOINT, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"client_credentials grant failed: {data}")
    return token


def gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": TOKEN,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    if "errors" in data and data["errors"]:
        raise RuntimeError(f"GraphQL error: {data['errors']}")
    return data["data"]


def shopifyql(q):
    """ShopifyQL を実行し rows（列名キーの dict 配列）を返す。"""
    data = gql(
        "query($q:String!){shopifyqlQuery(query:$q){parseErrors tableData{columns{name}rows}}}",
        {"q": q},
    )
    res = data["shopifyqlQuery"]
    if res.get("parseErrors"):
        raise RuntimeError(f"ShopifyQL parse error for [{q}]: {res['parseErrors']}")
    td = res.get("tableData")
    return td["rows"] if td else []


def money(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def intval(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def day_of(row):
    """TIMESERIES 行から日付(YYYY-MM-DD)を取り出す。"""
    v = row.get("day")
    if v is None and row:
        v = row[next(iter(row))]
    return str(v)[:10]


def main():
    global TOKEN
    TOKEN = fetch_access_token()

    now = datetime.datetime.now(JST)
    today = now.date()
    yesterday = today - datetime.timedelta(days=1)
    month_start = today.replace(day=1)
    # 今期＝事業年度（決算5月末・6/1開始）
    fy_start = (datetime.date(today.year, 6, 1)
                if today.month >= 6 else datetime.date(today.year - 1, 6, 1))
    iso = lambda d: d.isoformat()
    today_s, yday_s = iso(today), iso(yesterday)

    # --- 今期（6/1〜本日）売上・注文数 ---
    term_rows = shopifyql(f"FROM sales SHOW total_sales, orders SINCE {iso(fy_start)} UNTIL today")
    term_sales = money(term_rows[0]["total_sales"]) if term_rows else 0
    term_orders = intval(term_rows[0]["orders"]) if term_rows else 0
    aov = round(term_sales / term_orders) if term_orders else 0

    # --- 今月の日別売上（折れ線） ---
    trend_rows = shopifyql(f"FROM sales SHOW total_sales TIMESERIES day SINCE {iso(month_start)} UNTIL today")
    points = [{"date": day_of(r), "sales": money(r.get("total_sales"))} for r in trend_rows]
    month_sales_total = sum(p["sales"] for p in points)

    # --- 今月の販売点数合計 ---
    mu = shopifyql(f"FROM inventory SHOW inventory_units_sold SINCE {iso(month_start)} UNTIL today")
    month_units_total = intval(mu[0]["inventory_units_sold"]) if mu else 0

    # --- 本日／昨日 の販売点数・売上 ---
    units_by_day = {day_of(r): intval(r.get("inventory_units_sold"))
                    for r in shopifyql(f"FROM inventory SHOW inventory_units_sold TIMESERIES day SINCE {iso(yesterday)} UNTIL today")}
    sales_by_day = {day_of(r): money(r.get("total_sales"))
                    for r in shopifyql(f"FROM sales SHOW total_sales TIMESERIES day SINCE {iso(yesterday)} UNTIL today")}

    # --- 今月の人気アイテム TOP5（販売点数） ---
    top_rows = shopifyql(
        "FROM inventory SHOW inventory_units_sold GROUP BY product_title "
        f"ORDER BY inventory_units_sold DESC LIMIT 5 SINCE {iso(month_start)} UNTIL today"
    )

    # 商品名 → handle / featured 画像 を解決（active 商品をタイトル一致で引く）
    title_map = {}
    cursor = None
    for _ in range(8):  # 最大 ~400 件
        page = gql(
            'query($n:Int!,$after:String){products(first:$n,after:$after,query:"status:active"){'
            'pageInfo{hasNextPage endCursor} nodes{title handle featuredMedia{preview{image{url}}}}}}',
            {"n": 50, "after": cursor},
        )["products"]
        for n in page["nodes"]:
            img = (((n.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}).get("url")
            title_map[n["title"]] = {"handle": n.get("handle"), "thumb": img}
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    top5 = []
    for i, row in enumerate(top_rows):
        title = row.get("product_title") or ""
        meta = title_map.get(title, {})
        top5.append({
            "rank": i + 1,
            "title": title,
            "units": intval(row.get("inventory_units_sold")),
            "handle": meta.get("handle"),
            "thumb": meta.get("thumb"),
        })

    out = {
        "shop": SHOP_NAME,
        "updatedAt": now.replace(microsecond=0).isoformat(),
        "refreshMinutes": REFRESH_MIN,
        "term": {"sinceLabel": f"{fy_start.month}/{fy_start.day}〜",
                 "sales": term_sales, "orders": term_orders, "aov": aov},
        "month": {"salesTotal": month_sales_total,
                  "unitsTotal": month_units_total, "days": today.day},
        "trend": {"points": points},
        "daily": {
            "todayUnits": units_by_day.get(today_s, 0),
            "yesterdayUnits": units_by_day.get(yday_s, 0),
            "todaySales": sales_by_day.get(today_s, 0),
            "yesterdaySales": sales_by_day.get(yday_s, 0),
        },
        "top5": top5,
    }

    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"wrote {OUT_PATH}: 今期=¥{term_sales:,}({term_orders}件) / "
          f"今月=¥{month_sales_total:,}({month_units_total}点) / "
          f"本日={out['daily']['todayUnits']}点 / TOP5={len(top5)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
