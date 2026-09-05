#!/usr/bin/env python3
"""nyuukin-auto: Shopify Payments + Komoju 入金明細 週次 自動取得
発火: 毎週金曜 10:00 JST (GHA cron・cron-job.org)
格納:
  - Dropbox `⚫︎入金/2026/{月}/Shopify_YYYY-MM-DD.csv` + `KOMOJU_YYYY-MM-DD.csv`
  - Google Drive `H683800_株式会社Japan Made屋/入金/2026/{月}/` 同上

冪等: 既存ファイル名重複時 skip
"""
import argparse, urllib.request, urllib.parse, json, os, base64, csv, io, re, sys, tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---- 環境変数 ----
SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "9a6736-3.myshopify.com")
SHOPIFY_CLIENT_ID = os.environ["SHOPIFY_CLIENT_ID"]
SHOPIFY_CLIENT_SECRET = os.environ["SHOPIFY_CLIENT_SECRET"]
KOMOJU_SECRET_KEY = os.environ["KOMOJU_SECRET_KEY"]
DROPBOX_APP_KEY = os.environ["DROPBOX_APP_KEY"]
DROPBOX_APP_SECRET = os.environ["DROPBOX_APP_SECRET"]
DROPBOX_REFRESH_TOKEN = os.environ["DROPBOX_REFRESH_TOKEN"]
GOOGLE_DRIVE_TOKEN_JSON = os.environ["GOOGLE_DRIVE_TOKEN_JSON"]
CHATWORK_API_TOKEN = os.environ.get("CHATWORK_API_TOKEN", "")

# Drive
DRIVE_SHARED_DRIVE_ID = "0AEoD1iWSGDYGUk9PVA"
DRIVE_NYUUKIN_FOLDER_ID = "1S5XR3Rg1Ae1omEG0_CiAjaTKD_HIeXT-"  # 「入金」

# Dropbox
DROPBOX_NYUUKIN_ROOT = "/たくぞー/契約書/⚫︎入金"

# 通知
CHATWORK_MYCHAT = 218962687

# ---- Dropbox helper ----
_dropbox_token_cache = None
def dropbox_token():
    global _dropbox_token_cache
    if _dropbox_token_cache:
        return _dropbox_token_cache
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": DROPBOX_REFRESH_TOKEN,
    }).encode()
    auth = base64.b64encode(f"{DROPBOX_APP_KEY}:{DROPBOX_APP_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://api.dropboxapi.com/oauth2/token",
        data=body,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}
    )
    _dropbox_token_cache = json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]
    return _dropbox_token_cache

def dropbox_exists(path):
    req = urllib.request.Request(
        "https://api.dropboxapi.com/2/files/get_metadata",
        data=json.dumps({"path": path}).encode(),
        headers={"Authorization": f"Bearer {dropbox_token()}", "Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except urllib.error.HTTPError as e:
        if e.code == 409: return False
        raise

def dropbox_upload(path, content_bytes):
    req = urllib.request.Request(
        "https://content.dropboxapi.com/2/files/upload",
        data=content_bytes,
        headers={
            "Authorization": f"Bearer {dropbox_token()}",
            "Content-Type": "application/octet-stream",
            "Dropbox-API-Arg": json.dumps({"path": path, "mode": "add", "autorename": False, "mute": True})
        }
    )
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

# ---- Drive helper ----
_drive_service = None
def drive_service():
    global _drive_service
    if _drive_service: return _drive_service
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        f.write(GOOGLE_DRIVE_TOKEN_JSON)
        token_path = f.name
    creds = Credentials.from_authorized_user_file(token_path)
    _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service

def drive_get_or_create_folder(name, parent_id):
    """親配下から name 検索・存在すれば ID 返す・なければ作成"""
    q = f"'{parent_id}' in parents and name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    r = drive_service().files().list(q=q, supportsAllDrives=True, includeItemsFromAllDrives=True, fields="files(id,name)").execute()
    if r.get('files'):
        return r['files'][0]['id']
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    r = drive_service().files().create(body=body, supportsAllDrives=True, fields="id").execute()
    return r['id']

def drive_upload_bytes(filename, content_bytes, parent_id):
    """既存ファイル名あれば skip・なければ upload"""
    q = f"'{parent_id}' in parents and name='{filename}' and trashed=false"
    r = drive_service().files().list(q=q, supportsAllDrives=True, includeItemsFromAllDrives=True, fields="files(id,name)").execute()
    if r.get('files'):
        return {"skipped": True, "existing_id": r['files'][0]['id']}
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype="text/csv", resumable=False)
    r = drive_service().files().create(
        body={"name": filename, "parents": [parent_id]},
        media_body=media, supportsAllDrives=True, fields="id,name,size"
    ).execute()
    return {"skipped": False, "id": r['id']}

# ---- Shopify Payments ----
_shopify_token_cache = None
def shopify_token():
    global _shopify_token_cache
    if _shopify_token_cache: return _shopify_token_cache
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": SHOPIFY_CLIENT_ID,
        "client_secret": SHOPIFY_CLIENT_SECRET
    }).encode()
    req = urllib.request.Request(
        f"https://{SHOPIFY_STORE}/admin/oauth/access_token",
        data=body,
        headers={"Accept": "application/json"}
    )
    _shopify_token_cache = json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]
    return _shopify_token_cache

def shopify_gql(query):
    req = urllib.request.Request(
        f"https://{SHOPIFY_STORE}/admin/api/2026-01/graphql.json",
        data=json.dumps({"query": query}).encode(),
        headers={"X-Shopify-Access-Token": shopify_token(), "Content-Type": "application/json"}
    )
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

def fetch_shopify_payouts(days_lookback=14):
    """直近 N 日以内の PAID payouts 取得"""
    q = '''{
      shopifyPaymentsAccount {
        payouts(first: 10, reverse: true) {
          edges {
            node {
              id
              legacyResourceId
              issuedAt
              status
              net { amount currencyCode }
            }
          }
        }
      }
    }'''
    r = shopify_gql(q)
    payouts = [e['node'] for e in r['data']['shopifyPaymentsAccount']['payouts']['edges']]
    # 過去 N 日以内 + PAID or IN_TRANSIT 系
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_lookback)
    filtered = []
    for p in payouts:
        issued = datetime.fromisoformat(p['issuedAt'].replace('Z', '+00:00'))
        if issued >= cutoff:
            filtered.append(p)
    return filtered

def fetch_recent_balance_transactions(days_lookback):
    """直近 N 日の全 balanceTransactions 取得 (associatedPayout で group する用)
    ⚠ query="payout_id:XXX" filter は効かない (全期間返る) ため lookback で早期 break
    """
    all_tx = []
    cursor = None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_lookback)
    while True:
        after = f', after: "{cursor}"' if cursor else ''
        q = f'''{{
          shopifyPaymentsAccount {{
            balanceTransactions(first: 250, reverse: true{after}) {{
              pageInfo {{ hasNextPage endCursor }}
              edges {{
                cursor
                node {{
                  id
                  type
                  transactionDate
                  amount {{ amount currencyCode }}
                  fee {{ amount }}
                  net {{ amount }}
                  sourceType
                  sourceId
                  sourceOrderTransactionId
                  associatedOrder {{ id }}
                  associatedPayout {{ id status }}
                }}
              }}
            }}
          }}
        }}'''
        r = shopify_gql(q)
        conn = r['data']['shopifyPaymentsAccount']['balanceTransactions']
        older_found = False
        for e in conn['edges']:
            n = e['node']
            tdate = datetime.fromisoformat(n['transactionDate'].replace('Z', '+00:00'))
            if tdate < cutoff:
                older_found = True
                continue
            all_tx.append(n)
        if older_found or not conn['pageInfo']['hasNextPage']: break
        cursor = conn['pageInfo']['endCursor']
    return all_tx

def group_transactions_by_payout(all_tx):
    """associatedPayout.id → transactions list"""
    grouped = {}
    for tx in all_tx:
        ap = tx.get('associatedPayout')
        if not ap: continue
        pid = ap['id'].split('/')[-1]
        grouped.setdefault(pid, []).append(tx)
    return grouped

def build_shopify_csv(payout, transactions):
    """CSV 生成 (日付+金額+手数料の要件充足)"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Transaction Date", "Type", "Order ID", "Amount", "Fee", "Net", "Currency",
        "Payout Date", "Payout ID", "Payout Status", "Payout Net"
    ])
    for tx in transactions:
        order_id = tx.get('associatedOrder', {}).get('id', '') if tx.get('associatedOrder') else ''
        # gid://shopify/Order/1234567890 → 1234567890
        order_num = order_id.split('/')[-1] if order_id else ''
        w.writerow([
            tx['transactionDate'],
            tx['type'],
            order_num,
            tx['amount']['amount'],
            tx.get('fee', {}).get('amount', '0'),
            tx.get('net', {}).get('amount', '0'),
            tx['amount']['currencyCode'],
            payout['issuedAt'],
            payout['legacyResourceId'],
            payout['status'],
            payout['net']['amount'],
        ])
    return buf.getvalue().encode('utf-8')

# ---- Komoju ----
def komoju_get(path):
    auth = base64.b64encode(f"{KOMOJU_SECRET_KEY}:".encode()).decode()
    req = urllib.request.Request(
        f"https://komoju.com{path}",
        headers={"Authorization": f"Basic {auth}"}
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

def fetch_komoju_settlements(days_lookback=14):
    """直近 N 日以内の settlements 取得"""
    all_sett = []
    for page in range(1, 20):
        data = komoju_get(f"/api/v1/settlements?per_page=50&page={page}")
        all_sett.extend(data.get('data', []))
        if page >= data.get('last_page', 1): break
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_lookback)
    return [s for s in all_sett if datetime.fromisoformat(s['cutoff_time'].replace('Z','+00:00')) >= cutoff]

def download_komoju_csv(settlement):
    """settlement.download.csv URL から S3 経由で DL"""
    url = settlement['download']['csv']
    return urllib.request.urlopen(url, timeout=60).read()

# ---- 格納 ----
def month_folder_from_date(date_str):
    """'2026-09-04' → '09'"""
    dt = datetime.fromisoformat(date_str.replace('Z','+00:00'))
    return f"{dt.month:02d}"

def year_folder_from_date(date_str):
    dt = datetime.fromisoformat(date_str.replace('Z','+00:00'))
    return str(dt.year)

def date_only(date_str):
    """'2026-09-04T00:00:00Z' → '2026-09-04'"""
    return date_str.split('T')[0]

def ensure_drive_month_folder(year, month):
    """入金/{year}/{month}/ を確保して folder_id 返す"""
    year_id = drive_get_or_create_folder(year, DRIVE_NYUUKIN_FOLDER_ID)
    return drive_get_or_create_folder(month, year_id)

def store_file(source_name, date_str, filename, content_bytes, dry_run=False):
    """Dropbox+Drive 両方に格納 (冪等)"""
    year = year_folder_from_date(date_str)
    month = month_folder_from_date(date_str)
    dropbox_path = f"{DROPBOX_NYUUKIN_ROOT}/{year}/{month}/{filename}"
    print(f"  [{source_name}] {filename} ({len(content_bytes)}b)")
    print(f"    → Dropbox: {dropbox_path}")

    if dry_run:
        print("    (dry-run: 書き込みなし)")
        return {"dropbox": "dry-run", "drive": "dry-run"}

    # Dropbox 冪等
    if dropbox_exists(dropbox_path):
        print(f"    Dropbox: skip (既存)")
        drop_r = {"skipped": True}
    else:
        drop_r = dropbox_upload(dropbox_path, content_bytes)
        print(f"    Dropbox: uploaded")

    # Drive (冪等 upload)
    drive_month_id = ensure_drive_month_folder(year, month)
    drive_r = drive_upload_bytes(filename, content_bytes, drive_month_id)
    print(f"    Drive: {'skip (既存)' if drive_r['skipped'] else 'uploaded'}")

    return {"dropbox": drop_r, "drive": drive_r}

# ---- Chatwork ----
def notify_chatwork(body):
    if not CHATWORK_API_TOKEN: return
    data = urllib.parse.urlencode({"body": body, "self_unread": 1}).encode()
    req = urllib.request.Request(
        f"https://api.chatwork.com/v2/rooms/{CHATWORK_MYCHAT}/messages",
        data=data,
        headers={"X-ChatWorkToken": CHATWORK_API_TOKEN, "Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"[Chatwork通知失敗] {e}", file=sys.stderr)

# ---- Main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--source', choices=['shopify', 'komoju', 'both'], default='both')
    ap.add_argument('--days-lookback', type=int, default=14, help='何日以内の payout/settlement を対象にするか')
    args = ap.parse_args()

    print(f"=== nyuukin-auto 発火 ({datetime.now(timezone(timedelta(hours=9))).isoformat()}) ===")
    print(f"source={args.source} / dry_run={args.dry_run} / lookback={args.days_lookback}日")

    results = {"shopify": [], "komoju": [], "errors": []}

    # Shopify
    if args.source in ('shopify', 'both'):
        try:
            print("\n### Shopify Payments ###")
            payouts = fetch_shopify_payouts(days_lookback=args.days_lookback)
            print(f"対象 payouts: {len(payouts)}件")
            # 直近 balanceTransactions 一括取得 → payout ごとに group
            all_tx = fetch_recent_balance_transactions(days_lookback=args.days_lookback + 7)  # payout に含まれる古い tx も拾う
            grouped = group_transactions_by_payout(all_tx)
            print(f"直近 balanceTransactions: {len(all_tx)}件 / {len(grouped)} payouts に group")
            for p in payouts:
                pd = date_only(p['issuedAt'])
                filename = f"Shopify_{pd}.csv"
                transactions = grouped.get(p['legacyResourceId'], [])
                from collections import Counter
                type_counts = Counter(t['type'] for t in transactions)
                print(f"    Payout {pd} type内訳: {dict(type_counts)}  total={len(transactions)}")
                csv_bytes = build_shopify_csv(p, transactions)
                r = store_file("Shopify", p['issuedAt'], filename, csv_bytes, dry_run=args.dry_run)
                results["shopify"].append({"file": filename, "tx_count": len(transactions), "amount": p['net']['amount']})
        except Exception as e:
            print(f"❌ Shopify エラー: {e}", file=sys.stderr)
            results["errors"].append(f"Shopify: {e}")

    # Komoju
    if args.source in ('komoju', 'both'):
        try:
            print("\n### Komoju Settlements ###")
            settlements = fetch_komoju_settlements(days_lookback=args.days_lookback)
            print(f"対象 settlements: {len(settlements)}件")
            for s in settlements:
                pd = date_only(s['cutoff_time'])
                filename = f"KOMOJU_{pd}.csv"
                csv_bytes = download_komoju_csv(s)
                r = store_file("Komoju", s['cutoff_time'], filename, csv_bytes, dry_run=args.dry_run)
                results["komoju"].append({"file": filename, "size": len(csv_bytes), "amount": s['transaction_amount_cents']})
        except Exception as e:
            print(f"❌ Komoju エラー: {e}", file=sys.stderr)
            results["errors"].append(f"Komoju: {e}")

    # 結果サマリ
    print(f"\n=== 完了 ===")
    print(f"Shopify: {len(results['shopify'])}件 / Komoju: {len(results['komoju'])}件 / エラー: {len(results['errors'])}件")

    # 失敗時のみ Chatwork 通知 (§ 常駐スキル通知は失敗時のみ 準拠)
    if results['errors'] and not args.dry_run:
        notify_chatwork(f"[title]nyuukin-auto 失敗[/title]" + "\n".join(results['errors']))

    return 1 if results['errors'] else 0

if __name__ == "__main__":
    sys.exit(main())
