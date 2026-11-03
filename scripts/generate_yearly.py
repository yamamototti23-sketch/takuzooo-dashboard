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
BASE_MONTHLY_JSON = os.environ.get("BASE_MONTHLY_JSON", "docs/base_monthly.json")
OUT_PATH = os.environ.get("OUT_PATH", "docs/jm-yearly.json")

# freee 営業利益 (2023-06〜先月): freee-accounting スキル lib_freee.py 経由。
# GHA モードでは FREEE_ENV_PATH で env 場所を差替え可能。
FREEE_LIB_PATH = os.environ.get(
    "FREEE_LIB_PATH",
    "/Users/takuma/.claude/skills/freee-accounting/scripts",
)
# 会計期 (JM 6月始まり): fy=2023 → 2023-06〜2024-05
FREEE_FY_RANGES = [
    (2023, [(6, 12), (1, 5)]),
    (2024, [(6, 12), (1, 5)]),
    (2025, [(6, 12), (1, 5)]),
    (2026, [(6, 5)]),  # end_month は下記 main() で今日基準に動的計算
]

# 当月 partial 対応 (2026-11-03 追加・税理士原様が日々仕訳登録してくれるため)
# 1=有効 → 当月分 (1日〜昨日) の売上/利益を partial として rolling 計算は null のまま
#           単月値のみ months[] に追加・hero 期別累計に加算
# 0=無効 → 従来通り end_ym (前月末 or 前々月末) まで固定
INCLUDE_PARTIAL_CURRENT_MONTH = os.environ.get("INCLUDE_PARTIAL_CURRENT_MONTH", "0") == "1"


def aggregate_base_monthly():
    """BASE 月次売上 dict {YYYY-MM: sales_int} を返す。

    優先順位:
      1) docs/base_monthly.json が存在すれば読み取り (GHA / public repo 環境)
      2) BASE_CSV_FILES を Shift-JIS decode で直読み (Mac 対話モード)
    """
    # 案 D: 事前集計 JSON 経由 (GHA public repo 環境)
    if os.path.exists(BASE_MONTHLY_JSON):
        with open(BASE_MONTHLY_JSON, encoding="utf-8") as f:
            payload = json.load(f)
        monthly = {m["month"]: int(m["sales"]) for m in payload.get("months", [])}
        print(f"[BASE] loaded {len(monthly)} months from {BASE_MONTHLY_JSON}",
              file=sys.stderr)
        return monthly

    # Fallback: Mac 対話モード = BASE CSV 直読み
    monthly = defaultdict(int)
    found = 0
    for f in BASE_CSV_FILES:
        if not os.path.exists(f):
            continue
        found += 1
        with open(f, "rb") as fh:
            text = fh.read().decode("shift_jis", errors="replace")
        reader = csv.reader(io.StringIO(text))
        next(reader)
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
    if found == 0:
        raise RuntimeError(
            f"BASE データソース未発見: {BASE_MONTHLY_JSON} も BASE CSV も見つからない"
        )
    print(f"[BASE] loaded from {found} CSV files (Mac local)", file=sys.stderr)
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


def aggregate_shopify_monthly(start_ym: str, end_ym: str, end_date: str = None):
    """Shopify orders → 月次売上 dict {YYYY-MM: sales_int}

    start_ym / end_ym は YYYY-MM 形式 (両端含む)。
    currentTotalPriceSet (返金反映後・税込) を SUM。

    end_date (YYYY-MM-DD) 指定時 (2026-11-03 追加・partial 対応):
      当月の途中日まで集計したい時に使用。end_ym の月末日ではなく end_date で絞る。
      例: end_ym="2026-11", end_date="2026-11-02" → 11/1-11/2 分だけ集計。
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

    # end 日付の決定: end_date 明示 > end_ym の月末日
    if end_date:
        end_boundary = end_date
    else:
        end_year, end_month = map(int, end_ym.split("-"))
        if end_month == 12:
            end_last_day = 31
        else:
            first_next = datetime.date(end_year, end_month + 1, 1)
            end_last_day = (first_next - datetime.timedelta(days=1)).day
        end_boundary = f"{end_ym}-{end_last_day:02d}"

    query_str = (
        f"created_at:>={start_ym}-01 "
        f"created_at:<={end_boundary} "
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


def calc_rolling_12m_with_null(months_list, monthly_dict, min_data_ym: str):
    """rolling 12M・当該月含む直近12ヶ月に監視対象データ (freee 開始月以降) が
    全て揃っている月のみ計算。1つでも欠けたら None (freee 未登録期の混入防止)。
    """
    rolling = {}
    for i, ym in enumerate(months_list):
        if i < 11:
            rolling[ym] = None
            continue
        window = months_list[i - 11 : i + 1]
        # 全ての window 月が min_data_ym 以降 かつ dict にあるか
        if any(m < min_data_ym or monthly_dict.get(m) is None for m in window):
            rolling[ym] = None
        else:
            rolling[ym] = sum(monthly_dict.get(m, 0) for m in window)
    return rolling


def aggregate_freee_profit(end_ym: str, partial_end_date: str = None) -> tuple:
    """freee trial_pl API から 2023-06〜end_ym の月次経常利益 dict を返す。

    重要仕様 (2026-09-01 発見):
      freee `trial_pl` API は start_month を無視して「期首〜end_month の累計」を返す。
      単月値を得るには、当月累計 − 前月累計 の差分計算が必要。

    手順:
      1) 各会計期の 期首〜各月 累計値 (cum_by_ym) を fetch
      2) 単月 = 当月累計 − 前月累計 (期首月 6月は累計そのまま=期首単月)

    lib_freee.py (FreeeClient) 経由。取得失敗時は空 dict を返す (fail-soft)。

    partial_end_date (YYYY-MM-DD) 指定時 (2026-11-03 追加・原様の日次仕訳反映):
      当月分の期首〜partial_end_date 累計を取得し、
      partial_profit = partial_cum - 前月末累計 を算出。
      返り値 2つめの dict に {"ym":"2026-11","end_date":"2026-11-02","monthly_profit":...} を格納。

    Returns:
      (monthly_dict, partial_info or None)
    """
    try:
        sys.path.insert(0, FREEE_LIB_PATH)
        from lib_freee import FreeeClient  # type: ignore
    except Exception as e:
        print(f"[freee] lib import failed: {e} (skip profit)", file=sys.stderr)
        return {}, None

    try:
        c = FreeeClient()
    except Exception as e:
        print(f"[freee] client init failed: {e} (skip profit)", file=sys.stderr)
        return {}, None

    end_y, end_m = map(int, end_ym.split("-"))

    # Phase 1: 各月 累計値を fetch
    cum_by_ym = {}  # {"YYYY-MM": 期首〜当月の累計 経常利益}
    for fy in range(2023, end_y + 2):
        for m in list(range(6, 13)) + list(range(1, 6)):
            cal_y = fy if m >= 6 else fy + 1
            if (cal_y, m) > (end_y, end_m):
                break
            ym = f"{cal_y:04d}-{m:02d}"
            try:
                endpoint = (
                    f"/reports/trial_pl?company_id={c.company_id}"
                    f"&fiscal_year={fy}&start_month=6&end_month={m}"
                )
                res = c._request("GET", endpoint)
                bs = res["trial_pl"]["balances"]
                eig = next(
                    (b for b in bs if b.get("account_category_name") == "経常損益金額"),
                    None,
                )
                if eig is not None:
                    cum_by_ym[ym] = int(eig.get("closing_balance") or 0)
            except Exception as e:
                print(f"[freee] cum {ym} skip: {e}", file=sys.stderr)

    # Phase 2: 単月 = 当月累計 - 前月累計
    monthly = {}
    for fy in range(2023, end_y + 2):
        for i, m in enumerate(list(range(6, 13)) + list(range(1, 6))):
            cal_y = fy if m >= 6 else fy + 1
            if (cal_y, m) > (end_y, end_m):
                break
            ym = f"{cal_y:04d}-{m:02d}"
            if ym not in cum_by_ym:
                continue
            if m == 6:
                # 期首月 = 累計そのまま
                monthly[ym] = cum_by_ym[ym]
            else:
                # 期内前月 (6→7→8...→12→1→2...→5 の並び)
                prev_m = m - 1 if m > 1 else 12
                prev_cal_y = cal_y if m > 1 else cal_y - 1
                prev_ym = f"{prev_cal_y:04d}-{prev_m:02d}"
                if prev_ym in cum_by_ym:
                    monthly[ym] = cum_by_ym[ym] - cum_by_ym[prev_ym]
                else:
                    # 前月データ欠損 → 単月抽出不能 → skip
                    print(f"[freee] {ym} skip: prev_ym {prev_ym} missing", file=sys.stderr)
    print(f"[freee] {len(monthly)} months collected (単月・差分計算後)", file=sys.stderr)

    # Phase 3: 当月 partial (2026-11-03 追加・原様の日次仕訳反映)
    partial_info = None
    if partial_end_date:
        try:
            p_y, p_m, p_d = map(int, partial_end_date.split("-"))
            current_ym = f"{p_y:04d}-{p_m:02d}"
            fy = p_y if p_m >= 6 else p_y - 1
            term_start = f"{fy:04d}-06-01"
            # 当月累計 (期首〜昨日・start_date/end_date で日次指定・fiscal_year と排他)
            endpoint = (
                f"/reports/trial_pl?company_id={c.company_id}"
                f"&start_date={term_start}&end_date={partial_end_date}"
            )
            res = c._request("GET", endpoint)
            bs = res["trial_pl"]["balances"]
            eig = next(
                (b for b in bs if b.get("account_category_name") == "経常損益金額"),
                None,
            )
            if eig is not None:
                partial_cum = int(eig.get("closing_balance") or 0)
                # 前月末累計 = 当月単月 = 差分
                if p_m == 6:
                    # 期首月 = partial 累計そのまま = 当月単月
                    partial_profit = partial_cum
                else:
                    prev_m = p_m - 1 if p_m > 1 else 12
                    prev_cal_y = p_y if p_m > 1 else p_y - 1
                    prev_ym = f"{prev_cal_y:04d}-{prev_m:02d}"
                    prev_cum = cum_by_ym.get(prev_ym)
                    if prev_cum is None:
                        print(
                            f"[freee] partial skip: prev_ym {prev_ym} missing in cum_by_ym",
                            file=sys.stderr,
                        )
                        partial_profit = None
                    else:
                        partial_profit = partial_cum - prev_cum
                if partial_profit is not None:
                    partial_info = {
                        "ym": current_ym,
                        "end_date": partial_end_date,
                        "monthly_profit": partial_profit,
                    }
                    print(
                        f"[freee] partial {current_ym} (〜{partial_end_date}): ¥{partial_profit:,}",
                        file=sys.stderr,
                    )
        except Exception as e:
            print(f"[freee] partial fetch skip: {e}", file=sys.stderr)

    return monthly, partial_info


def prev_year_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{y - 1:04d}-{m:02d}"


def main():
    now = datetime.datetime.now(JST)
    today = now.date()

    # 最新月 = 今日から YEARLY_LAG_MONTHS ヶ月前の月末
    # 旧 (UAO・2026-08-31 終了) = ラグ 2ヶ月
    # 新 (原様・2026-09-01 開始・毎日仕訳登録) = ラグ 1ヶ月
    # さらに INCLUDE_PARTIAL_CURRENT_MONTH=1 で当月分 (昨日まで) を partial として追加
    lag_months = int(os.environ.get("YEARLY_LAG_MONTHS", "2"))
    target = today.replace(day=1)
    for _ in range(lag_months):
        target = (target - datetime.timedelta(days=1)).replace(day=1)
    end_ym = f"{target.year:04d}-{target.month:02d}"

    # 当月 partial 情報 (INCLUDE_PARTIAL_CURRENT_MONTH=1 の時のみ使用)
    partial_end_date = None
    partial_current_ym = None
    if INCLUDE_PARTIAL_CURRENT_MONTH:
        yesterday = today - datetime.timedelta(days=1)
        partial_end_date = yesterday.isoformat()
        partial_current_ym = f"{yesterday.year:04d}-{yesterday.month:02d}"
        # 昨日が前月の最終日 (月の1日実行) なら partial 不要 (end_ym 側で確定)
        if partial_current_ym == end_ym:
            print(
                f"[main] partial skip: yesterday={partial_end_date} is in end_ym={end_ym}",
                file=sys.stderr,
            )
            partial_end_date = None
            partial_current_ym = None

    # BASE 全期間 + Shopify 開始月推定
    print("[1/3] BASE 集計中...", file=sys.stderr)
    base_monthly = aggregate_base_monthly()
    print(f"  BASE: {len(base_monthly)} months (¥{sum(base_monthly.values()):,})",
          file=sys.stderr)

    # Shopify 集計範囲: end_ym まで + partial があれば当月まで
    shopify_end_ym = partial_current_ym if partial_current_ym else end_ym
    print(f"[2/3] Shopify 集計中 (2026-02〜{shopify_end_ym}, partial_end={partial_end_date})...",
          file=sys.stderr)
    shopify_monthly = aggregate_shopify_monthly(
        "2026-02", shopify_end_ym, end_date=partial_end_date
    )
    print(f"  Shopify: {len(shopify_monthly)} months (¥{sum(shopify_monthly.values()):,})",
          file=sys.stderr)

    # 合算
    print("[3/3] 合算 + rolling 12M 計算...", file=sys.stderr)
    all_months = set(base_monthly) | set(shopify_monthly)
    start_ym = min(all_months)  # "2021-05"
    # months_list の末尾: partial があれば当月まで含める (rolling は null で描画)
    months_list_end = partial_current_ym if partial_current_ym else end_ym
    months_list = month_range(start_ym, months_list_end)

    monthly_sales = {}
    for ym in months_list:
        monthly_sales[ym] = base_monthly.get(ym, 0) + shopify_monthly.get(ym, 0)

    rolling = calc_rolling_12m(months_list, monthly_sales)
    # 当月 partial は rolling 定義 (12ヶ月末合計) から外す
    if partial_current_ym and partial_current_ym in rolling:
        rolling[partial_current_ym] = None

    # --- freee 営業利益 (2023-06〜end_ym + 当月 partial) 統合 ---
    print(f"[3.5/3] freee 営業利益 集計中 (2023-06〜{end_ym}" +
          (f" + partial 当月〜{partial_end_date}" if partial_end_date else "") + ")...",
          file=sys.stderr)
    profit_monthly, partial_profit_info = aggregate_freee_profit(
        end_ym, partial_end_date=partial_end_date
    )
    # 当月 partial を profit_monthly に merge (rolling 12M は未計算のまま)
    if partial_profit_info:
        profit_monthly[partial_profit_info["ym"]] = partial_profit_info["monthly_profit"]

    # rolling 12M profit (freee 未登録の 2023-05 以前を含む window は None)
    rolling_profit = calc_rolling_12m_with_null(months_list, profit_monthly, "2023-06")
    # 当月 partial は rolling 定義から外す
    if partial_current_ym and partial_current_ym in rolling_profit:
        rolling_profit[partial_current_ym] = None

    # 前年同月の rolling を紐付け
    months_out = []
    for ym in months_list:
        r_current = rolling[ym]
        r_prev = rolling.get(prev_year_month(ym))
        is_partial = (ym == partial_current_ym)
        entry = {
            "month": ym,
            "monthly_sales": monthly_sales[ym],
            "rolling12m": r_current,
            "rolling12m_prev": r_prev,
            "monthly_profit": profit_monthly.get(ym),
            "rolling12m_profit": rolling_profit.get(ym),
        }
        if is_partial:
            entry["is_partial"] = True
            entry["partial_end_date"] = partial_end_date
        months_out.append(entry)

    # 最新月の YoY% (rolling 12M・partial 月は rolling null なので end_ym 月で計算)
    latest = months_out[-1]
    latest_for_yoy = latest
    if latest.get("is_partial"):
        # partial 月の rolling は null → 1つ前 (end_ym 月) を使う
        for m in reversed(months_out[:-1]):
            if m["rolling12m"] is not None:
                latest_for_yoy = m
                break
    if latest_for_yoy["rolling12m"] and latest_for_yoy["rolling12m_prev"]:
        yoy_pct = round(
            (latest_for_yoy["rolling12m"] - latest_for_yoy["rolling12m_prev"])
            / latest_for_yoy["rolling12m_prev"] * 100,
            1,
        )
    else:
        yoy_pct = None

    # --- 期別累計 (hero 表示用・6月始まり期・売上+営業利益) ---
    # partial 月があれば term_end_ym = 当月 (期首〜昨日まで)
    # なければ従来通り end_ym (前月末) まで
    hero_end_ym = partial_current_ym if partial_current_ym else end_ym
    end_y, end_m = map(int, hero_end_ym.split("-"))
    if end_m >= 6:
        term_start_y = end_y
        term_label = "今期"
    else:
        term_start_y = end_y - 1
        term_label = "前期"
    term_start_ym = f"{term_start_y:04d}-06"
    term_end_ym = hero_end_ym

    # 今期 range と前年同期 range (同月数)
    term_months = [m for m in months_list if term_start_ym <= m <= term_end_ym]
    prev_term_start_ym = f"{term_start_y - 1:04d}-06"
    prev_term_end_y = end_y - 1
    prev_term_end_ym = f"{prev_term_end_y:04d}-{end_m:02d}"
    prev_term_months = [m for m in months_list if prev_term_start_ym <= m <= prev_term_end_ym]

    # 売上 (常に完全: BASE + Shopify で全月データあり)
    term_sales_sum = sum(monthly_sales.get(m, 0) for m in term_months) if term_months else None
    prev_term_sales_sum = sum(monthly_sales.get(m, 0) for m in prev_term_months) if prev_term_months else None
    if term_sales_sum is not None and prev_term_sales_sum:
        term_yoy_pct = round(
            (term_sales_sum - prev_term_sales_sum) / prev_term_sales_sum * 100, 1
        )
    else:
        term_yoy_pct = None

    # 営業利益 (freee 未登録期間は None・欠損月あれば None)
    term_profit_values = [profit_monthly.get(m) for m in term_months if profit_monthly.get(m) is not None]
    if len(term_profit_values) == len(term_months) and term_months:
        term_profit_sum = sum(term_profit_values)
    else:
        term_profit_sum = None
    prev_term_profit_values = [profit_monthly.get(m) for m in prev_term_months if profit_monthly.get(m) is not None]
    prev_term_profit_sum = (
        sum(prev_term_profit_values)
        if len(prev_term_profit_values) == len(prev_term_months) and prev_term_months
        else None
    )

    out = {
        "shop": SHOP_NAME,
        "updatedAt": now.replace(microsecond=0).isoformat(),
        "definition": "移動年計 (rolling 12M) = 当該月末の直近12ヶ月合計。売上=BASE+Shopify、営業利益=freee (2023-06〜)。当月は partial (期首〜昨日累計)。",
        "months": months_out,
        "latest": {
            "month": latest["month"],
            "rolling12m": latest["rolling12m"],
            "rolling12m_prev": latest["rolling12m_prev"],
            "yoy_pct": yoy_pct,
            "rolling12m_profit": latest.get("rolling12m_profit"),
            "monthly_profit": latest.get("monthly_profit"),
            # partial 月フラグ (当月途中なら true・UI で点線表示)
            "is_partial": latest.get("is_partial", False),
            "partial_end_date": latest.get("partial_end_date"),
            # 期別累計 (hero 用・今期 or 前期通期・partial 月があれば昨日まで)
            "term_label": term_label,
            "term_start_month": term_start_ym,
            "term_end_month": term_end_ym,
            "term_sales_sum": term_sales_sum,
            "term_profit_sum": term_profit_sum,
            # 前年同期
            "term_prev_start_month": prev_term_start_ym,
            "term_prev_end_month": prev_term_end_ym,
            "term_prev_sales_sum": prev_term_sales_sum,
            "term_prev_profit_sum": prev_term_profit_sum,
            "term_yoy_pct": term_yoy_pct,
        },
    }

    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"\n✓ wrote {OUT_PATH}", file=sys.stderr)
    print(f"  期間: {months_list[0]} 〜 {months_list[-1]} ({len(months_list)}ヶ月)", file=sys.stderr)
    if latest.get("is_partial"):
        print(f"  最新月: {latest['month']} partial (〜{latest.get('partial_end_date')})"
              f" 売上¥{latest['monthly_sales']:,} 利益¥{(latest.get('monthly_profit') or 0):,}",
              file=sys.stderr)
        print(f"  rolling12m 基準月: {latest_for_yoy['month']} ¥{(latest_for_yoy['rolling12m'] or 0):,}",
              file=sys.stderr)
    else:
        print(f"  最新 rolling12m: ¥{(latest['rolling12m'] or 0):,}", file=sys.stderr)
    if yoy_pct is not None:
        print(f"  前年同月比: {yoy_pct:+.1f}%", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
