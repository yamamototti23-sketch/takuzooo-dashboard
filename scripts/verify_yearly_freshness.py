#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify-yearly workflow: docs/jm-yearly.json updatedAt が 30h 以内か検証。
古ければ Chatwork マイチャット通知 + exit 1 (workflow を failure 扱い)。
"""
import json
import os
import sys
import datetime
import urllib.request
import urllib.parse

ROOM_ID = 218962687
YEARLY_PATH = "docs/jm-yearly.json"
THRESHOLD_HOURS = 30  # 24h + 余裕6h


def notify(body: str):
    token = os.environ["CHATWORK_API_TOKEN"]
    data = urllib.parse.urlencode({"body": body, "self_unread": "1"}).encode()
    req = urllib.request.Request(
        f"https://api.chatwork.com/v2/rooms/{ROOM_ID}/messages",
        data=data,
        headers={"X-ChatWorkToken": token},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "yamamototti23-sketch/takuzooo-dashboard")
    with open(YEARLY_PATH, encoding="utf-8") as f:
        d = json.load(f)
    updated_str = d["updatedAt"]
    try:
        updated = datetime.datetime.fromisoformat(updated_str)
    except ValueError:
        print(f"⚠ updatedAt parse failed: {updated_str}", file=sys.stderr)
        sys.exit(1)
    if updated.tzinfo is None:
        # tz なし表記は JST 想定 (generate_yearly.py は JST で出力)
        updated = updated.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
    now = datetime.datetime.now(datetime.timezone.utc)
    hours_ago = (now - updated).total_seconds() / 3600
    print(f"updatedAt: {updated_str} ({hours_ago:.1f}h ago)")

    if hours_ago > THRESHOLD_HOURS:
        body = (
            f"[info][title]⚠ 年計 発火漏れ検知 (Layer 4)[/title]"
            f"docs/jm-yearly.json の updatedAt が {hours_ago:.1f}h 前です。\n"
            f"昨日 (毎月1日 09:00 JST) の update-yearly workflow が"
            f"発火しなかった可能性があります。\n"
            f"\n"
            f"即座に手動発火してください:\n"
            f"https://github.com/{repo}/actions/workflows/update-yearly.yml\n"
            f"\n"
            f"原因調査:\n"
            f"- Run 履歴: https://github.com/{repo}/actions\n"
            f"- GHA schedule は稀に遅延 or skip される (workflow_dispatch 手動発火が最短復旧)"
            f"[/info]"
        )
        notify(body)
        print(f"⚠ 発火漏れ通知送信完了 ({hours_ago:.1f}h ago)")
        sys.exit(1)
    else:
        print(f"✓ updated {hours_ago:.1f}h ago - OK (silent)")


if __name__ == "__main__":
    main()
