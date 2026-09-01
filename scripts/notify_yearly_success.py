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
    latest_str = f"¥{L['rolling12m']:,}"
    prev_str = f"¥{L['rolling12m_prev']:,}" if L.get("rolling12m_prev") else "—"
    yoy = f"{L['yoy_pct']:+.1f}%" if L.get("yoy_pct") is not None else "—"

    # 次回発火予定 (JST)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    next_y, next_m = now.year, now.month + 1
    if next_m > 12:
        next_y += 1
        next_m = 1
    next_str = f"{next_y}/{next_m}/1 09:00 JST"

    body = (
        f"[info][title]年計 月次更新完了 ({int(m_year)}年{int(m_month)}月末)[/title]"
        f"移動年計 (直近12ヶ月合計): {latest_str}\n"
        f"前年同月: {prev_str}\n"
        f"前年比: {yoy}\n"
        f"\n"
        f"次回発火予定: {next_str}\n"
        f"Kiosk: https://yamamototti23-sketch.github.io/takuzooo-dashboard/kiosk.html\n"
        f"Yearly 単体: https://yamamototti23-sketch.github.io/takuzooo-dashboard/jm-yearly.html"
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
