#!/usr/bin/env python3
"""Fetch YouTube channel metrics and write docs/data.json.

Required environment variables:
  YT_CLIENT_ID
  YT_CLIENT_SECRET
  YT_REFRESH_TOKEN

Optional:
  YT_CHANNEL_ID  (default: UCjSeXbXh2BS-ErZO7a6ienQ — Takuzooo)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

CHANNEL_ID = os.environ.get("YT_CHANNEL_ID", "UCjSeXbXh2BS-ErZO7a6ienQ")
GOAL_TARGET = 300000  # 登録者ゴール
CLIENT_ID = os.environ["YT_CLIENT_ID"]
CLIENT_SECRET = os.environ["YT_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["YT_REFRESH_TOKEN"]

OUTPUT = Path(__file__).resolve().parent / "docs" / "data.json"


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


def fetch_channel_snapshot(token: str) -> dict:
    data = api_get(
        "https://www.googleapis.com/youtube/v3/channels",
        {"part": "snippet,statistics,contentDetails", "id": CHANNEL_ID},
        token,
    )
    items = data.get("items") or []
    if not items:
        raise RuntimeError(f"channel not found: {CHANNEL_ID}")
    c = items[0]
    stats = c.get("statistics", {})
    return {
        "title": c["snippet"]["title"],
        "thumbnail": c["snippet"]["thumbnails"]["default"]["url"],
        "subscribers": int(stats.get("subscriberCount", 0)),
        "total_views": int(stats.get("viewCount", 0)),
        "total_videos": int(stats.get("videoCount", 0)),
        "uploads_playlist": c["contentDetails"]["relatedPlaylists"]["uploads"],
    }


def fetch_analytics(token: str, start: date, end: date) -> dict:
    """Daily breakdown + totals via YouTube Analytics API."""
    base = "https://youtubeanalytics.googleapis.com/v2/reports"
    common = {
        "ids": f"channel=={CHANNEL_ID}",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
    }
    daily = api_get(
        base,
        {
            **common,
            "metrics": "views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost",
            "dimensions": "day",
            "sort": "day",
        },
        token,
    )
    cols = [h["name"] for h in daily.get("columnHeaders", [])]
    rows = daily.get("rows") or []
    daily_rows = [dict(zip(cols, row)) for row in rows]

    totals = {
        "views": sum(int(r.get("views", 0)) for r in daily_rows),
        "watch_minutes": sum(int(r.get("estimatedMinutesWatched", 0)) for r in daily_rows),
        "subs_gained": sum(int(r.get("subscribersGained", 0)) for r in daily_rows),
        "subs_lost": sum(int(r.get("subscribersLost", 0)) for r in daily_rows),
    }
    totals["subs_net"] = totals["subs_gained"] - totals["subs_lost"]
    if daily_rows:
        avg_dur_total = sum(
            int(r.get("averageViewDuration", 0)) * int(r.get("views", 0)) for r in daily_rows
        )
        totals["avg_view_duration_sec"] = (
            int(avg_dur_total / totals["views"]) if totals["views"] else 0
        )
    else:
        totals["avg_view_duration_sec"] = 0
    return {"daily": daily_rows, "totals": totals}


def fetch_top_videos(token: str, start: date, end: date, top: int = 10) -> list[dict]:
    data = api_get(
        "https://youtubeanalytics.googleapis.com/v2/reports",
        {
            "ids": f"channel=={CHANNEL_ID}",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": "views,estimatedMinutesWatched,averageViewDuration",
            "dimensions": "video",
            "sort": "-views",
            "maxResults": top,
        },
        token,
    )
    cols = [h["name"] for h in data.get("columnHeaders", [])]
    rows = [dict(zip(cols, row)) for row in data.get("rows") or []]
    if not rows:
        return []

    video_ids = [r["video"] for r in rows]
    meta = api_get(
        "https://www.googleapis.com/youtube/v3/videos",
        {"part": "snippet,statistics,contentDetails", "id": ",".join(video_ids)},
        token,
    )
    meta_by_id = {it["id"]: it for it in meta.get("items") or []}

    out = []
    for r in rows:
        vid = r["video"]
        m = meta_by_id.get(vid, {})
        sn = m.get("snippet", {})
        st = m.get("statistics", {})
        out.append({
            "video_id": vid,
            "title": sn.get("title", "(unknown)"),
            "published_at": sn.get("publishedAt"),
            "thumbnail": (sn.get("thumbnails") or {}).get("medium", {}).get("url"),
            "views_period": int(r.get("views", 0)),
            "watch_minutes_period": int(r.get("estimatedMinutesWatched", 0)),
            "avg_view_duration_sec": int(r.get("averageViewDuration", 0)),
            "views_total": int(st.get("viewCount", 0)) if st else None,
            "likes_total": int(st.get("likeCount", 0)) if st and "likeCount" in st else None,
            "comments_total": int(st.get("commentCount", 0)) if st and "commentCount" in st else None,
        })
    return out


def fetch_recent_uploads(token: str, uploads_playlist: str, limit: int = 10) -> list[dict]:
    data = api_get(
        "https://www.googleapis.com/youtube/v3/playlistItems",
        {"part": "snippet,contentDetails", "playlistId": uploads_playlist, "maxResults": limit},
        token,
    )
    items = data.get("items") or []
    if not items:
        return []
    video_ids = [it["contentDetails"]["videoId"] for it in items]
    stats = api_get(
        "https://www.googleapis.com/youtube/v3/videos",
        {"part": "statistics,snippet", "id": ",".join(video_ids)},
        token,
    )
    stats_by_id = {it["id"]: it for it in stats.get("items") or []}

    out = []
    for it in items:
        vid = it["contentDetails"]["videoId"]
        sn = it["snippet"]
        s = stats_by_id.get(vid, {})
        st = s.get("statistics", {})
        out.append({
            "video_id": vid,
            "title": sn.get("title"),
            "published_at": sn.get("publishedAt"),
            "thumbnail": (sn.get("thumbnails") or {}).get("medium", {}).get("url"),
            "views_total": int(st.get("viewCount", 0)) if "viewCount" in st else None,
            "likes_total": int(st.get("likeCount", 0)) if "likeCount" in st else None,
            "comments_total": int(st.get("commentCount", 0)) if "commentCount" in st else None,
        })
    return out


def main() -> int:
    token = get_access_token()
    today = date.today()
    end = today - timedelta(days=1)
    start_28 = end - timedelta(days=27)

    snapshot = fetch_channel_snapshot(token)
    analytics_28 = fetch_analytics(token, start_28, end)
    top_videos = fetch_top_videos(token, start_28, end, top=10)
    recent = fetch_recent_uploads(token, snapshot["uploads_playlist"], limit=10)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel_id": CHANNEL_ID,
        "channel": snapshot,
        "goal": {
            "target": GOAL_TARGET,
            "current": snapshot["subscribers"],
            "remaining": max(0, GOAL_TARGET - snapshot["subscribers"]),
            "progress_pct": round(snapshot["subscribers"] / GOAL_TARGET * 100, 2) if GOAL_TARGET else None,
        },
        "period": {"start": start_28.isoformat(), "end": end.isoformat(), "days": 28},
        "analytics_28d": analytics_28,
        "top_videos_28d": top_videos,
        "recent_uploads": recent,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
