#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""update-yearly workflow 成功時の Chatwork マイチャット通知"""
import json
import os
import datetime
import urllib.request
import urllib.parse

ROOM_ID = 218962687  # たくぞうマイチャット
YEARLY_PATH = "docs/jm-yearly.json"


def main():
    with open(YEARLY_PATH, encoding="utf-8") as f:
        d = json.load(f)
    L = d["latest"]
    m_year, m_month = L["month"].split("-")

    # 移動年計 (rolling 12M・売上)
    rolling_str = f"¥{L['rolling12m']:,}"
    yoy_rolling = f"{L['yoy_pct']:+.1f}%" if L.get("yoy_pct") is not None else "—"

    # 期別累計 (hero と同じ・売上+経常利益)
    ts_ym = lambda ym: f"{int(ym.split('-')[0])}/{int(ym.split('-')[1])}"
    term_range = f"{ts_ym(L['term_start_month'])}〜{ts_ym(L['term_end_month'])}"
    term_sales = f"¥{L['term_sales_sum']:,}" if L.get("term_sales_sum") is not None else "—"
    if L.get("term_profit_sum") is not None:
        p = L["term_profit_sum"]
        term_profit = ("−¥" if p < 0 else "¥") + f"{abs(p):,}"
    else:
        term_profit = "—"
    term_yoy = f"{L['term_yoy_pct']:+.1f}%" if L.get("term_yoy_pct") is not None else "—"

    # 次回発火予定 (JST)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    next_y, next_m = now.year, now.month + 1
    if next_m > 12:
        next_y += 1
        next_m = 1
    next_str = f"{next_y}/{next_m}/1 09:00 JST"

    body = (
        f"[info][title]年計 月次更新完了 ({int(m_year)}年{int(m_month)}月末時点)[/title]"
        f"■ {L['term_label']} ({term_range}) 累計\n"
        f"  売上: {term_sales}\n"
        f"  経常利益: {term_profit}\n"
        f"  前年同期比: {term_yoy}\n"
        f"\n"
        f"■ 移動年計 (直近12ヶ月合計・売上)\n"
        f"  {rolling_str} (前年同月比 {yoy_rolling})\n"
        f"\n"
        f"次回発火: {next_str}\n"
        f"Kiosk: https://yamamototti23-sketch.github.io/takuzooo-dashboard/kiosk.html"
        f"[/info]"
    )

    token = os.environ["CHATWORK_API_TOKEN"]
    data = urllib.parse.urlencode({"body": body, "self_unread": "1"}).encode()
    req = urllib.request.Request(
        f"https://api.chatwork.com/v2/rooms/{ROOM_ID}/messages",
        data=data,
        headers={"X-ChatWorkToken": token},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()
    print("✓ Chatwork 成功通知送信完了")


if __name__ == "__main__":
    main()
