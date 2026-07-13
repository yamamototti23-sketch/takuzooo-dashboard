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

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

def is_members_only(video_id):
    """Detect members-only gated content.
    Data API returns privacyStatus='public' for members-only videos, so /watch page scraping
    is the only reliable signal. Look for playabilityStatus.status=UNPLAYABLE + membership hint.
    Simple Mozilla/5.0 UA gets a JS-fallback stub page — need a full browser UA + Accept-Language.
    Fail-safe: return False on any error (fallback to excluded_video_ids.txt).
    """
    try:
        req = urllib.request.Request(f"https://www.youtube.com/watch?v={video_id}",
                                     headers={"User-Agent": _UA, "Accept-Language": "ja,en;q=0.5"})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")
        if '"status":"UNPLAYABLE"' in html and ('メンバー' in html or 'members-only' in html.lower()):
            return True
    except Exception as e:
        print(f"is_members_only({video_id}) fail-soft: {e}")
    return False

def slim(v): return {"date":v["date"],"views":v["views"],"title":v["title"],
                     "thumb":v["thumb"],"type":v["type"],"videoId":v["id"]}
def rankrow(v,i): return {"rank":i+1,"title":v["title"],"thumb":v["thumb"],
                          "likes":v["likes"],"comments":v["comments"],"views":v["views"],"type":v["type"]}

def main():
    token = access_token()
    ch = data_api(token, "channels", part="statistics,contentDetails,snippet", id=CHANNEL_ID)["items"][0]
    st = ch["statistics"]; uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    total_videos = int(st["videoCount"]); subs_rounded = int(st["subscriberCount"])
    avatar = ch["snippet"]["thumbnails"]["high"]["url"]; name = ch["snippet"]["title"]
    ch_start = ch["snippet"]["publishedAt"][:10]

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

    # Analytics確定の末日から逆7日（B案）。窓はデータが進めば自動でスライドする。
    views7d = []
    daily_views_by_day = {}
    if daily:
        end_day = dt.date.fromisoformat(daily[-1]["day"])
        daily_views_by_day = {r["day"]: r["views"] for r in daily}
        for i in range(6, -1, -1):
            d = (end_day - dt.timedelta(days=i)).isoformat()
            views7d.append({"date": d, "views": int(daily_views_by_day.get(d, 0))})
    views7d_total = sum(x["views"] for x in views7d)
    if views7d:
        print(f"views7d: {views7d_total} total over {views7d[0]['date']}..{views7d[-1]['date']} (Analytics末日基準)")
    else:
        print("views7d: empty (no Analytics data)")

    # 昨年同期7日 (絶対厳守: 今年 views7d の日付範囲を1年前にずらすだけ)
    views7d_lastyear = []
    if views7d:
        for entry in views7d:
            d_this = dt.date.fromisoformat(entry["date"])
            try:
                d_ly = d_this.replace(year=d_this.year - 1)
            except ValueError:  # 2/29 → 2/28
                d_ly = d_this.replace(year=d_this.year - 1, day=max(1, d_this.day - 1))
            iso = d_ly.isoformat()
            views7d_lastyear.append({"date": iso, "views": int(daily_views_by_day.get(iso, 0))})
    views7d_lastyear_total = sum(x["views"] for x in views7d_lastyear)
    if views7d_lastyear:
        print(f"views7d_lastyear: {views7d_lastyear_total} total over {views7d_lastyear[0]['date']}..{views7d_lastyear[-1]['date']}")

    def w_(days):
        rows = daily if days is None else [r for r in daily if r["day"] >= (today-dt.timedelta(days=days)).isoformat()]
        return {"views":sum(r["views"] for r in rows), "likes":sum(r["likes"] for r in rows),
                "comments":sum(r["comments"] for r in rows),
                "subscribersDelta":sum(r["gained"]-r["lost"] for r in rows)}
    subs_exact = sum(r["gained"]-r["lost"] for r in daily) if daily else subs_rounded

    def shift_year(iso_date, delta=-1):
        """YYYY-MM-DD を delta 年ずらす。2/29 は 2/28 に丸める。"""
        y, m, d = int(iso_date[:4])+delta, int(iso_date[5:7]), int(iso_date[8:10])
        try:
            return dt.date(y, m, d)
        except ValueError:
            return dt.date(y, m, max(1, d-1))

    WIN = {"1週間":7,"2週間":14,"1ヶ月":30,"3ヶ月":90,"6ヶ月":180,"1年":365,"全期間":None}
    periods = {}
    for k, w in WIN.items():
        agg = w_(w) if daily else {"views":0,"likes":0,"comments":0,"subscribersDelta":None}
        sel = vids if k=="全期間" else [v for v in vids if days_ago(v["publishedAt"]) <= w]
        agg["videos"] = total_videos if k=="全期間" else len(sel)
        pts = sorted(sel, key=lambda v: v["date"])[-CHART_CAP:]
        rnk = sorted([v for v in sel if v["type"]=="long"], key=lambda v:-v["views"])[:20]

        # 昨年同期間 (絶対厳守: 今年 pts の左端-右端の月日 を 1年前 に貼った窓に該当する動画のみ)
        # 「全期間」は昨年比較の意味薄いのでスキップ (=空配列)
        pts_ly = []
        if w is not None and pts:
            t0_ly = shift_year(pts[0]["date"])
            t1_ly = shift_year(pts[-1]["date"])
            sel_ly = []
            for v in vids:
                try:
                    vd = dt.date.fromisoformat(v["date"])
                except ValueError:
                    continue
                if t0_ly <= vd <= t1_ly:
                    sel_ly.append(v)
            pts_ly = sorted(sel_ly, key=lambda v: v["date"])[-CHART_CAP:]

        periods[k] = {**agg,
                      "chart":{"points":[slim(v) for v in pts],
                               "points_lastyear":[slim(v) for v in pts_ly]},
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
    # Step1: privacyStatus + manual excluded list を弾く (公開動画のみ・unlisted/private 除外)
    longs_public = [v for v in longs if v.get("privacyStatus")=="public" and v["id"] not in excluded]
    # Step2: 先頭から members-only 自動検知して打ち切る (通常1-2リクエストで確定)
    latest = None
    scanned = 0
    for v in longs_public:
        scanned += 1
        if is_members_only(v["id"]):
            print(f"  [members-only detected] skip {v['id']} '{v['title'][:40]}'")
            continue
        latest = v; break
    pre_id = longs[0]["id"] if longs else None
    post_id = latest["id"] if latest else None
    print(f"latestLong filter: pre(any)={pre_id} -> post(public)={post_id} "
          f"(longs={len(longs)} public={len(longs_public)} excluded={len(excluded)} scanned={scanned})")
    if latest is None:
        raise RuntimeError("No non-members public long video found (all filtered out)")
    dd = max(1, days_ago(latest["publishedAt"]))
    last10 = [v for v in longs_public if v["id"] != latest["id"]][:9]
    last10 = [latest] + last10
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
           "views7d":views7d, "views7d_total":views7d_total,
           "views7d_lastyear":views7d_lastyear, "views7d_lastyear_total":views7d_lastyear_total,
           "latestLong":latestLong, "periods":periods}
    os.makedirs("docs", exist_ok=True)
    json.dump(out, open(OUT,"w"), ensure_ascii=False)
    print("wrote", OUT, "videos", len(vids), "analytics_days", len(daily))

if __name__ == "__main__":
    main()
