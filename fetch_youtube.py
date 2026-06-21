#!/usr/bin/env python3
"""Takuzooo Realtime Dashboard — hybrid fetcher (OAuth only, no API key).
Data API v3       = 構造 / 各動画の現在値 / 最新動画のライブ再生数
YouTube Analytics = 期間ごとの正確な視聴・高評価・コメント・登録増減（日別を1回取得して合算）
出力: docs/data.json （GitHub Pages がそのまま配信）
必要な環境変数: YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN
"""
import os, re, json, datetime as dt, urllib.request, urllib.parse, urllib.error

CLIENT_ID     = os.environ["YT_CLIENT_ID"]
CLIENT_SECRET = os.environ["YT_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["YT_REFRESH_TOKEN"]
CHANNEL_ID = "UCjSeXbXh2BS-ErZO7a6ienQ"
GOAL_TARGET, SHORT_MAX_SEC, REFRESH_MIN, CHART_CAP = 300000, 60, 60, 60
HISTORY_KEEP = 25  # 24 bars need 25 snapshots (N+1 → N diffs)
OUT = "docs/data.json"
JST = dt.timezone(dt.timedelta(hours=9))
DATA_API  = "https://www.googleapis.com/youtube/v3/"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"

def access_token():
    body = urllib.parse.urlencode({"client_id":CLIENT_ID,"client_secret":CLIENT_SECRET,
        "refresh_token":REFRESH_TOKEN,"grant_type":"refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=body), timeout=30) as r:
        return json.load(r)["access_token"]

def data_api(token, ep, **p):
    req = urllib.request.Request(DATA_API+ep+"?"+urllib.parse.urlencode(p),
                                 headers={"Authorization":"Bearer "+token})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)

def analytics_daily(token, start, end):
    q = urllib.parse.urlencode({"ids":"channel==MINE","startDate":start,"endDate":end,
        "metrics":"views,likes,comments,subscribersGained,subscribersLost",
        "dimensions":"day","sort":"day","maxResults":10000})
    req = urllib.request.Request(ANALYTICS+"?"+q, headers={"Authorization":"Bearer "+token})
    with urllib.request.urlopen(req, timeout=40) as r:
        rows = json.load(r).get("rows", [])
    return [{"day":x[0],"views":x[1],"likes":x[2],"comments":x[3],"gained":x[4],"lost":x[5]} for x in rows]

def dur_sec(s):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    h, mi, se = (int(x) if x else 0 for x in m.groups()); return h*3600+mi*60+se

def slim(v): return {"date":v["date"],"views":v["views"],"title":v["title"],
                     "thumb":v["thumb"],"type":v["type"],"videoId":v["id"]}
def rankrow(v,i): return {"rank":i+1,"title":v["title"],"thumb":v["thumb"],
                          "likes":v["likes"],"comments":v["comments"],"views":v["views"],"type":v["type"]}

def viewers24h_compute(prev_history, current_total, now_jst):
    """Append a snapshot, keep last HISTORY_KEEP points, return (history, viewers24h).
    bars = consecutive diffs (max 0). label = ending-hour of each bar in JST."""
    history = list(prev_history or [])
    history.append({"ts": now_jst.isoformat(timespec="seconds"), "total": int(current_total)})
    history = history[-HISTORY_KEEP:]
    bars = []
    for i in range(1, len(history)):
        a = int(history[i-1]["total"]); b = int(history[i]["total"])
        try:
            ts = dt.datetime.fromisoformat(history[i]["ts"])
        except Exception:
            ts = now_jst
        bars.append({"label": f"{ts.hour}時", "v": max(0, b - a)})
    bars = bars[-24:]
    total = sum(b["v"] for b in bars)
    return history, {"total": total, "bars": bars,
                     "note": "毎時の累計差分（YouTubeはバッチ更新のため0や急増が混在）"}

def main():
    token = access_token()
    ch = data_api(token, "channels", part="statistics,contentDetails,snippet", id=CHANNEL_ID)["items"][0]
    st = ch["statistics"]; uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    total_videos = int(st["videoCount"]); subs_rounded = int(st["subscriberCount"])
    total_views_now = int(st.get("viewCount", 0))
    avatar = ch["snippet"]["thumbnails"]["high"]["url"]; name = ch["snippet"]["title"]
    ch_start = ch["snippet"]["publishedAt"][:10]

    prev_history = []
    try:
        with open(OUT) as f:
            prev_history = json.load(f).get("viewers24h_history", []) or []
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    now_jst = dt.datetime.now(JST)
    history, viewers24h = viewers24h_compute(prev_history, total_views_now, now_jst)

    ids, pg = [], None
    while True:
        r = data_api(token, "playlistItems", part="contentDetails", playlistId=uploads, maxResults=50,
                     **({"pageToken":pg} if pg else {}))
        ids += [it["contentDetails"]["videoId"] for it in r["items"]]; pg = r.get("nextPageToken")
        if not pg: break

    vids = []
    for i in range(0, len(ids), 50):
        r = data_api(token, "videos", part="statistics,snippet,contentDetails,status", id=",".join(ids[i:i+50]), maxResults=50)
        for it in r["items"]:
            if it["contentDetails"]["duration"] == "P0D": continue  # skip live broadcasts (3 known)
            s = it.get("statistics", {})
            vids.append({"id":it["id"], "title":it["snippet"]["title"],
                "publishedAt":it["snippet"]["publishedAt"], "date":it["snippet"]["publishedAt"][:10],
                "thumb":it["snippet"]["thumbnails"].get("medium",{}).get("url"),
                "views":int(s.get("viewCount",0)), "likes":int(s.get("likeCount",0)),
                "comments":int(s.get("commentCount",0)),
                "privacyStatus":it.get("status",{}).get("privacyStatus",""),
                "type":"short" if dur_sec(it["contentDetails"]["duration"])<=SHORT_MAX_SEC else "long"})

    now = dt.datetime.now(dt.timezone.utc)
    days_ago = lambda iso: (now - dt.datetime.fromisoformat(iso.replace("Z","+00:00"))).days
    today = dt.datetime.now(JST).date()

    daily = []
    try:
        daily = analytics_daily(token, ch_start, today.isoformat())
    except urllib.error.HTTPError as e:
        print("Analytics error:", e.read().decode()[:300])

    def w_(days):
        rows = daily if days is None else [r for r in daily if r["day"] >= (today-dt.timedelta(days=days)).isoformat()]
        return {"views":sum(r["views"] for r in rows), "likes":sum(r["likes"] for r in rows),
                "comments":sum(r["comments"] for r in rows),
                "subscribersDelta":sum(r["gained"]-r["lost"] for r in rows)}
    subs_exact = sum(r["gained"]-r["lost"] for r in daily) if daily else subs_rounded

    WIN = {"1週間":7,"2週間":14,"1ヶ月":30,"3ヶ月":90,"6ヶ月":180,"1年":365,"全期間":None}
    periods = {}
    for k, w in WIN.items():
        agg = w_(w) if daily else {"views":0,"likes":0,"comments":0,"subscribersDelta":None}
        sel = vids if k=="全期間" else [v for v in vids if days_ago(v["publishedAt"]) <= w]
        agg["videos"] = total_videos if k=="全期間" else len(sel)
        pts = sorted(sel, key=lambda v: v["date"])[-CHART_CAP:]
        rnk = sorted([v for v in sel if v["type"]=="long"], key=lambda v:-v["views"])[:20]
        periods[k] = {**agg, "chart":{"points":[slim(v) for v in pts]},
                      "ranking":[rankrow(v,i) for i,v in enumerate(rnk)]}

    longs = sorted([v for v in vids if v["type"]=="long"], key=lambda v:v["publishedAt"], reverse=True)
    # latestLong: public-only (unlisted/private/members-only are excluded).
    # Optional manual exclude list at config/excluded_video_ids.txt (one ID per line).
    excluded = set()
    try:
        with open("config/excluded_video_ids.txt") as f:
            excluded = {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    except FileNotFoundError:
        pass
    longs_public = [v for v in longs if v.get("privacyStatus")=="public" and v["id"] not in excluded]
    pre_id  = longs[0]["id"] if longs else None
    post_id = longs_public[0]["id"] if longs_public else None
    print(f"latestLong filter: pre(any)={pre_id} -> post(public)={post_id} "
          f"(longs={len(longs)} public={len(longs_public)} excluded={len(excluded)})")
    latest = longs_public[0]; dd = max(1, days_ago(latest["publishedAt"])); last10 = longs_public[:10]
    speed = sorted(last10, key=lambda v:-(v["views"]/max(1,days_ago(v["publishedAt"]))))
    latestLong = {**slim(latest), "likes":latest["likes"], "comments":latest["comments"],
                  "publishedDaysAgo":days_ago(latest["publishedAt"]),
                  "viewsPerDay":round(latest["views"]/dd),
                  "speedRank":speed.index(latest)+1, "speedTotal":len(last10)}

    out = {"channel":{"name":name,"channelId":CHANNEL_ID,"avatar":avatar},
           "updatedAt":dt.datetime.now(JST).isoformat(timespec="seconds"),
           "refreshMinutes":REFRESH_MIN, "defaultPeriod":"1ヶ月",
           "subscribers":subs_exact,
           "subscribersDelta28d": w_(28)["subscribersDelta"] if daily else None,
           "goal":{"target":GOAL_TARGET,"current":subs_exact},
           "viewers24h":viewers24h, "viewers24h_history":history,
           "latestLong":latestLong, "periods":periods}
    os.makedirs("docs", exist_ok=True)
    json.dump(out, open(OUT,"w"), ensure_ascii=False)
    print("wrote", OUT, "videos", len(vids), "analytics_days", len(daily))

if __name__ == "__main__":
    main()
