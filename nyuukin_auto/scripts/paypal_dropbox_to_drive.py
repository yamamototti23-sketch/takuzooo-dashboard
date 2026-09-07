#!/usr/bin/env python3
"""PayPal CSV Dropbox → Drive 自動コピー
発火: 毎月 1-4日 9/10/11/12 JST + 5日 10:00 JST (GHA cron・月17run)

たくぞう作業 (月初):
  PayPal 管理画面から前月分の取引履歴 CSV を export
  Dropbox `/たくぞー/契約書/⚫︎入金/{年}/{月}/PayPal_YYYY-MM-DD.csv` に手動格納

CC 自動:
  1. Dropbox 該当月フォルダを list_folder
  2. `PayPal_*.csv` filter
  3. Drive `H683800_.../入金/{年}/{月}/` に未 upload のもののみ upload (冪等)
  4. 成功 silent / 失敗時マイチャット通知
"""
import argparse, os, sys, urllib.request, urllib.parse, json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# main.py の helper 流用
sys.path.insert(0, str(Path(__file__).parent))
from main import (
    dropbox_token,
    drive_get_or_create_folder, drive_upload_bytes,
    notify_chatwork,
    CHATWORK_MYCHAT,
    DROPBOX_NYUUKIN_ROOT, DRIVE_NYUUKIN_FOLDER_ID,
)


def dropbox_list_folder(path):
    """指定 Dropbox フォルダの直下 entry 一覧取得。存在しないフォルダは [] 返す"""
    req = urllib.request.Request(
        "https://api.dropboxapi.com/2/files/list_folder",
        data=json.dumps({
            "path": path,
            "recursive": False,
            "include_deleted": False,
        }).encode(),
        headers={
            "Authorization": f"Bearer {dropbox_token()}",
            "Content-Type": "application/json",
        }
    )
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return r.get('entries', [])
    except urllib.error.HTTPError as e:
        if e.code == 409:  # フォルダ不在 or path_not_found
            return []
        raise


def dropbox_download(path):
    """Dropbox からファイル bytes DL"""
    req = urllib.request.Request(
        "https://content.dropboxapi.com/2/files/download",
        headers={
            "Authorization": f"Bearer {dropbox_token()}",
            "Dropbox-API-Arg": json.dumps({"path": path}),
        }
    )
    return urllib.request.urlopen(req, timeout=60).read()


def ensure_drive_month_folder_paypal(year, month):
    """Drive 「入金/{year}/{month}/」を確保して folder_id 返す"""
    year_id = drive_get_or_create_folder(year, DRIVE_NYUUKIN_FOLDER_ID)
    return drive_get_or_create_folder(month, year_id)


def resolve_target_month(now_jst):
    """発火日から対象月を決定
    月初 1-5日 発火 → 前月分を対象
    """
    if now_jst.month == 1:
        return now_jst.year - 1, 12
    return now_jst.year, now_jst.month - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='実 upload なし')
    ap.add_argument('--year', type=int, default=None, help='対象年 (default: 前月)')
    ap.add_argument('--month', type=int, default=None, help='対象月 (default: 前月)')
    args = ap.parse_args()

    now_jst = datetime.now(timezone(timedelta(hours=9)))

    if args.year is None or args.month is None:
        target_year, target_month = resolve_target_month(now_jst)
    else:
        target_year, target_month = args.year, args.month

    year_str = str(target_year)
    month_str = f"{target_month:02d}"

    print(f"=== PayPal Dropbox→Drive 同期 ===")
    print(f"発火時刻: {now_jst.isoformat()}")
    print(f"対象月: {year_str}/{month_str}")
    print(f"dry-run: {args.dry_run}")

    dropbox_folder = f"{DROPBOX_NYUUKIN_ROOT}/{year_str}/{month_str}"
    results = {"copied": [], "skipped": [], "errors": []}

    try:
        entries = dropbox_list_folder(dropbox_folder)
        paypal_files = [
            e for e in entries
            if e.get('.tag') == 'file'
            and e.get('name', '').startswith('PayPal_')
            and e.get('name', '').lower().endswith('.csv')
        ]
        print(f"\nDropbox {dropbox_folder}")
        print(f"  → entry 総数: {len(entries)}")
        print(f"  → PayPal_*.csv: {len(paypal_files)} 件")

        if not paypal_files:
            print("\n(PayPal_*.csv なし・skip して正常終了)")
            return 0

        drive_month_id = ensure_drive_month_folder_paypal(year_str, month_str)
        print(f"\nDrive 「入金/{year_str}/{month_str}/」folder_id: {drive_month_id}")

        for entry in paypal_files:
            fname = entry['name']
            fpath = entry['path_lower']
            print(f"\n  [{fname}]")
            try:
                content_bytes = dropbox_download(fpath)
                print(f"    Dropbox DL: {len(content_bytes)} bytes")
                if args.dry_run:
                    print(f"    (dry-run: Drive upload skip)")
                    results['copied'].append(fname)
                    continue
                r = drive_upload_bytes(fname, content_bytes, drive_month_id)
                if r.get('skipped'):
                    print(f"    Drive: skip (既存 id={r.get('existing_id')})")
                    results['skipped'].append(fname)
                else:
                    print(f"    Drive: uploaded (id={r.get('id')})")
                    results['copied'].append(fname)
            except Exception as e:
                err = f"{fname}: {type(e).__name__}: {e}"
                print(f"    ❌ {err}", file=sys.stderr)
                results['errors'].append(err)

        print(f"\n=== 完了 ===")
        print(f"copied: {len(results['copied'])}")
        print(f"skipped: {len(results['skipped'])}")
        print(f"errors: {len(results['errors'])}")

    except Exception as e:
        err = f"全体エラー: {type(e).__name__}: {e}"
        print(f"❌ {err}", file=sys.stderr)
        results['errors'].append(err)

    # 通知 (成功時 silent / 失敗時のみマイチャット・§ 常駐スキル通知は失敗時のみ)
    if args.dry_run:
        pass  # dry-run 時は通知なし
    elif results['errors']:
        body = (
            "[title]⚠ PayPal CSV Dropbox→Drive 同期失敗[/title]\n"
            + "\n".join(results['errors'])
            + f"\n\n対象: {year_str}/{month_str}\n"
            + f"Dropbox: {dropbox_folder}\n"
            + "復旧: workflow_dispatch で再実行\n"
            + "https://github.com/yamamototti23-sketch/takuzooo-dashboard/actions/workflows/nyuukin_auto_paypal.yml"
        )
        notify_chatwork(CHATWORK_MYCHAT, body)

    return 1 if results['errors'] else 0


if __name__ == "__main__":
    sys.exit(main())
