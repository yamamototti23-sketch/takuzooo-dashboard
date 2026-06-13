#!/usr/bin/env python3
"""Fetch YouTube channel metrics for the AKIYA-style dashboard.

Writes `docs/data.json` with multiple periods (1週間/1ヶ月/3ヶ月/6ヶ月/1年/全期間).
Watch-time / average-view-duration intentionally NOT included.

Required env: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN
Optional   : YT_CHANNEL_ID (default Takuzooo)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

CHANNEL_ID = os.environ.get("YT_CHANNEL_ID", "UCjSeXbXh2BS-ErZO7a6ienQ")
GOAL_TARGET = 300000

CLIENT_ID = os.environ["YT_CLIENT_ID"]
CLIENT_SECRET = os.environ["YT_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["YT_REFRESH_TOKEN"]

OUTPUT = Path(__file__).resolve().parent / "docs" / "data.json"

# (key, label, days) — days=None means since channel start
PERIODS = [
    ("1w", "1週間", 7),
    ("1m", "1ヶ月", 30),
    ("3m", "3ヶ月", 90),
    ("6m", "6ヶ月", 180),
    ("1y", "1年", 365),
    ("all", "全期間", None),
]


def thumb_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def get_access_token() -> str:
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def api_get(url: str, params: dict, token: str) -> dict:
    r = requests.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"[ERROR] {url} -> {r.status_code}: {r.text[:500]}", file=sys.stderr)
        r.raise_for_status()
    return r.json()


def fetch_channel(token: str) -> dict:
    data = api_get(
        "https://www.googleapis.com/youtube/v3/channels",
        {"part": "snippet,statistics,contentDetails", "id": CHANNEL_ID},
        token,
    )
    items = data.get("items") or []
    if not items:
        raise RuntimeError(f"channel not found: {CHANNEL_ID}")
    c = items[0]
    sn = c["snippet"]
    st = c.get("statistics", {})
    return {
        "title": sn["title"],
        "thumbnail": sn["thumbnails"]["default"]["url"],
        "published_at": sn["publishedAt"],
        "subscribers": int(st.get("subscriberCount", 0)),
        "total_views": int(st.get("viewCount", 0)),
        "total_videos": int(st.get("videoCount", 0)),
        "uploads_playlist": c["contentDetails"]["relatedPlaylists"]["uploads"],
    }


def fetch_daily(token: str, start: date, end: date) -> list[dict]:
    data = api_get(
        "https://youtubeanalytics.googleapis.com/v2/reports",
        {
            "ids": f"channel=={CHANNEL_ID}",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": "views,subscribersGained,subscribersLost",
            "dimensions": "day",
            "sort": "day",
        },
        token,
    )
    cols = [h["name"] for h in data.get("columnHeaders", [])]
    return [dict(zip(cols, row)) for row in (data.get("rows") or [])]


def fetch_top_videos(token: str, start: date, end: date, top: int = 10) -> list[dict]:
    data = api_get(
        "https://youtubeanalytics.googleapis.com/v2/reports",
        {
            "ids": f"channel=={CHANNEL_ID}",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": "views",
            "dimensions": "video",
            "sort": "-views",
            "maxResults": top,
        },
        token,
    )
    cols = [h["name"] for h in data.get("columnHeaders", [])]
    rows = [dict(zip(cols, row)) for row in (data.get("rows") or [])]
    return [{"video_id": r["video"], "views": int(r.get("views", 0))} for r in rows]


def fetch_video_meta(token: str, video_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        if not chunk:
            continue
        data = api_get(
            "https://www.googleapis.com/youtube/v3/videos",
            {"part": "snippet,statistics", "id": ",".join(chunk)},
            token,
        )
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            st = it.get("statistics", {})
            out[it["id"]] = {
                "title": sn.get("title", "(unknown)"),
                "published_at": sn.get("publishedAt"),
                "thumbnail": thumb_url(it["id"]),
                "views_total": int(st["viewCount"]) if "viewCount" in st else None,
            }
    return out


def downsample(rows: list[dict], max_points: int) -> list[dict]:
    """Bucket-sum rows[day,views,subs*] down to <= max_points buckets."""
    n = len(rows)
    if n <= max_points or n == 0:
        return rows
    bucket = max(1, (n + max_points - 1) // max_points)
    out: list[dict] = []
    for i in range(0, n, bucket):
        chunk = rows[i:i + bucket]
        out.append({
            "day": chunk[0]["day"],
            "views": sum(int(r.get("views", 0)) for r in chunk),
            "subscribersGained": sum(int(r.get("subscribersGained", 0)) for r in chunk),
            "subscribersLost": sum(int(r.get("subscribersLost", 0)) for r in chunk),
        })
    return out


def main() -> int:
    token = get_access_token()
    today = date.today()
    end = today - timedelta(days=1)

    channel = fetch_channel(token)
    channel_start = datetime.fromisoformat(channel["published_at"].replace("Z", "+00:00")).date()

    periods: dict[str, dict] = {}
    all_top_ids: set[str] = set()

    for key, label, days in PERIODS:
        if days is None:
            start = channel_start
        else:
            start = max(channel_start, end - timedelta(days=days - 1))

        daily = fetch_daily(token, start, end)
        top = fetch_top_videos(token, start, end, top=10)
        for t in top:
            all_top_ids.add(t["video_id"])

        totals_views = sum(int(r.get("views", 0)) for r in daily)
        subs_gained = sum(int(r.get("subscribersGained", 0)) for r in daily)
        subs_lost = sum(int(r.get("subscribersLost", 0)) for r in daily)

        # downsample longer periods so the chart stays light
        chart_rows = downsample(daily, max_points=120)

        periods[key] = {
            "label": label,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": (end - start).days + 1,
            "totals": {
                "views": totals_views,
                "subs_gained": subs_gained,
                "subs_lost": subs_lost,
                "subs_net": subs_gained - subs_lost,
            },
            "chart": chart_rows,
            "top_videos": top,  # video_id + views; meta merged below
        }

    meta = fetch_video_meta(token, sorted(all_top_ids))
    for p in periods.values():
        merged: list[dict] = []
        for t in p["top_videos"]:
            m = meta.get(t["video_id"], {})
            merged.append({
                "video_id": t["video_id"],
                "views": t["views"],
                "title": m.get("title", "(unknown)"),
                "published_at": m.get("published_at"),
                "thumbnail": m.get("thumbnail", thumb_url(t["video_id"])),
                "views_total": m.get("views_total"),
            })
        p["top_videos"] = merged

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel_id": CHANNEL_ID,
        "channel": channel,
        "goal": {
            "target": GOAL_TARGET,
            "current": channel["subscribers"],
            "remaining": max(0, GOAL_TARGET - channel["subscribers"]),
            "progress_pct": round(channel["subscribers"] / GOAL_TARGET * 100, 2) if GOAL_TARGET else None,
        },
        "periods": periods,
        "period_order": [k for k, _, _ in PERIODS],
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
