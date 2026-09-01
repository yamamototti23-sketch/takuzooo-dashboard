"""
freee API wrapper (Python標準ライブラリのみ)

- OAuth トークン自動refresh
- wallet_txns / deals / user_matchers の CRUD
- Rate limit 対応 (300req/5min)

Env vars:
  FREEE_ACCESS_TOKEN, FREEE_REFRESH_TOKEN, FREEE_CLIENT_ID,
  FREEE_CLIENT_SECRET, FREEE_REDIRECT_URI, FREEE_COMPANY_ID
"""
import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from typing import Optional


BASE_URL = "https://api.freee.co.jp/api/1"
TOKEN_URL = "https://accounts.secure.freee.co.jp/public_api/token"
# 2026-08-04 恒久: GHA runner でも動作するよう env override サポート
# GHA workflow.yml で FREEE_ENV_PATH=/home/runner/... を set した時に GHA path を使う。
ENV_PATH_DEFAULT = Path(os.environ.get("FREEE_ENV_PATH") or (Path.home() / "JM自動化" / "freee-accounting" / ".env"))


class FreeeError(Exception):
    def __init__(self, code, body):
        self.code = code
        self.body = body
        super().__init__(f"HTTP {code}: {body[:200]}")


class FreeeClient:
    def __init__(self, env_path: Path = ENV_PATH_DEFAULT):
        self.env_path = env_path
        self._load_env()

    def _load_env(self):
        lines = self.env_path.read_text().splitlines()
        self._env_lines = lines
        self.kv = {}
        for l in lines:
            if '=' in l and not l.startswith('#'):
                k, v = l.split('=', 1)
                self.kv[k.strip()] = v.strip()
        self.company_id = int(self.kv.get('FREEE_COMPANY_ID', '10818043'))

    def _save_env(self, new_at: str, new_rt: str):
        new_lines = []
        for l in self._env_lines:
            if l.startswith('FREEE_ACCESS_TOKEN='):
                new_lines.append(f'FREEE_ACCESS_TOKEN={new_at}')
            elif l.startswith('FREEE_REFRESH_TOKEN='):
                new_lines.append(f'FREEE_REFRESH_TOKEN={new_rt}')
            else:
                new_lines.append(l)
        self.env_path.write_text('\n'.join(new_lines) + '\n')
        self._load_env()

    def refresh_token(self):
        """Refresh access token using refresh_token. Raises FreeeError on failure."""
        data = urllib.parse.urlencode({
            'grant_type': 'refresh_token',
            'client_id': self.kv['FREEE_CLIENT_ID'],
            'client_secret': self.kv['FREEE_CLIENT_SECRET'],
            'refresh_token': self.kv['FREEE_REFRESH_TOKEN'],
        }).encode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                tok = json.loads(res.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors='replace')
            raise FreeeError(e.code, body)
        self._save_env(tok['access_token'], tok['refresh_token'])
        return tok

    def _headers(self, method='GET'):
        h = {
            'Authorization': f"Bearer {self.kv['FREEE_ACCESS_TOKEN']}",
            'Accept': 'application/json',
        }
        if method in ('POST', 'PUT'):
            h['Content-Type'] = 'application/json'
        return h

    def _request(self, method, path, params=None, body=None, auto_refresh=True):
        url = f"{BASE_URL}{path}"
        if params:
            qs = urllib.parse.urlencode(params)
            url = f"{url}?{qs}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=self._headers(method), method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                text = res.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            body_bytes = e.read()
            try:
                body_str = body_bytes.decode(errors='replace')
            except Exception:
                body_str = str(body_bytes)
            if e.code == 401 and auto_refresh:
                # try refresh
                self.refresh_token()
                return self._request(method, path, params, body, auto_refresh=False)
            raise FreeeError(e.code, body_str)

    # === Convenience methods ===
    def get_wallet_txns(self, walletable_type=None, walletable_id=None,
                        start_date=None, end_date=None, status=None,
                        offset=0, limit=100):
        """GET /wallet_txns"""
        p = {'company_id': self.company_id, 'offset': offset, 'limit': limit}
        if walletable_type: p['walletable_type'] = walletable_type
        if walletable_id: p['walletable_id'] = walletable_id
        if start_date: p['start_date'] = start_date
        if end_date: p['end_date'] = end_date
        if status is not None: p['status'] = status
        return self._request('GET', '/wallet_txns', params=p)

    def get_all_wallet_txns(self, start_date=None, end_date=None, status=None):
        """Fetch all wallet_txns with pagination."""
        all_txns = []
        offset = 0
        while True:
            body = self.get_wallet_txns(start_date=start_date, end_date=end_date,
                                       status=status, offset=offset, limit=100)
            batch = body.get('wallet_txns', [])
            all_txns.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
            time.sleep(0.3)
            if offset > 5000:
                break
        return all_txns

    def get_deals(self, start_issue_date=None, end_issue_date=None,
                  offset=0, limit=100, **kwargs):
        p = {'company_id': self.company_id, 'offset': offset, 'limit': limit}
        if start_issue_date: p['start_issue_date'] = start_issue_date
        if end_issue_date: p['end_issue_date'] = end_issue_date
        p.update(kwargs)
        return self._request('GET', '/deals', params=p)

    def get_deal(self, deal_id, accruals='with'):
        p = {'company_id': self.company_id, 'accruals': accruals}
        return self._request('GET', f'/deals/{deal_id}', params=p)

    def post_deal(self, deal_body):
        """POST /deals. deal_body must include company_id, issue_date, type, details, etc."""
        if 'company_id' not in deal_body:
            deal_body['company_id'] = self.company_id
        return self._request('POST', '/deals', body=deal_body)

    def delete_deal(self, deal_id):
        return self._request('DELETE', f'/deals/{deal_id}?company_id={self.company_id}')

    def get_user_matchers(self, offset=0, limit=100):
        p = {'company_id': self.company_id, 'offset': offset, 'limit': limit}
        return self._request('GET', '/user_matchers', params=p)

    def get_all_user_matchers(self):
        all_rules = []
        offset = 0
        while True:
            body = self.get_user_matchers(offset=offset, limit=100)
            batch = body.get('data', [])
            all_rules.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
            time.sleep(0.3)
            if offset > 2000:
                break
        return all_rules

    def put_user_matcher(self, rule_id, body):
        """PUT /user_matchers/{id}
        絶対恒久ルール (2026-07-18): act=0 (取引を推測する) は自動発火しないので警告出力。
        """
        act_val = body.get('act')
        if act_val == 0:
            import warnings
            warnings.warn(
                f"[user_matcher PUT WARN] act=0 (推測モード=自動発火しない) で id={rule_id} を更新しようとしています。"
                f"自動発火させたい場合は act=1(経費登録) / act=3(振替登録) / act=4(無視登録) / act=12(プライベート登録) を指定してください。"
                f"CLAUDE.md § freee user_matcher act 値の正しい意味 参照。"
            )
        return self._request('PUT', f'/user_matchers/{rule_id}', body=body)

    def post_user_matcher(self, body, auto_act1=True):
        """POST /user_matchers
        絶対恒久ルール (2026-07-18): act 未指定なら auto_act1=True で act=1 (取引を登録する=自動発火) を強制セット。
        act=0 を明示指定した場合は警告のみ (発火しないので通常使わない)。
        CLAUDE.md § freee user_matcher act 値の正しい意味 参照。
        """
        if 'act' not in body or body.get('act') is None:
            if auto_act1:
                body = {**body, 'act': 1}
            else:
                raise ValueError("act フィールド未指定・auto_act1=False の場合は act を明示指定してください (0=推測/1=登録/3=振替登録/4=無視登録/12=プライベート登録)")
        elif body.get('act') == 0:
            import warnings
            warnings.warn(
                "[user_matcher POST WARN] act=0 (推測モード=自動発火しない) で POST しようとしています。"
                "自動発火させたい場合は act=1(経費登録) / act=3(振替登録) / act=4(無視登録) / act=12(プライベート登録) を指定してください。"
                "CLAUDE.md § freee user_matcher act 値の正しい意味 参照。"
            )
        p = {'company_id': self.company_id}
        # body に company_id が入っていなければ補完
        if 'company_id' not in body:
            body = {**body, 'company_id': self.company_id}
        return self._request('POST', '/user_matchers', body=body)

    def get_account_items(self):
        p = {'company_id': self.company_id}
        return self._request('GET', '/account_items', params=p)

    def get_taxes_codes(self):
        p = {'company_id': self.company_id}
        return self._request('GET', '/taxes/codes', params=p)

    def get_partners(self, offset=0, limit=100):
        p = {'company_id': self.company_id, 'offset': offset, 'limit': limit}
        return self._request('GET', '/partners', params=p)

    # === 領収書 (Receipt) 関連 (R16・2026-07-11 追加) ===
    def get_receipts(self, start_date=None, end_date=None, offset=0, limit=100):
        """GET /receipts - 領収書一覧"""
        p = {'company_id': self.company_id, 'offset': offset, 'limit': limit}
        if start_date: p['start_date'] = start_date
        if end_date: p['end_date'] = end_date
        return self._request('GET', '/receipts', params=p)

    def post_receipt(self, file_path, memo='', issue_date=None,
                     document_type='receipt'):
        """
        POST /receipts - ファイルアップロード (multipart/form-data)

        document_type: receipt (領収書) / bill (請求書) / other
        """
        import mimetypes
        import uuid
        import urllib.request
        boundary = f"----FormBoundary{uuid.uuid4().hex}"
        with open(file_path, 'rb') as f:
            file_data = f.read()
        mime, _ = mimetypes.guess_type(str(file_path))
        mime = mime or 'application/octet-stream'
        filename = Path(file_path).name

        body_parts = []
        def add_field(name, value):
            body_parts.append(f'--{boundary}\r\n'.encode())
            body_parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body_parts.append(f'{value}\r\n'.encode())

        add_field('company_id', str(self.company_id))
        add_field('description', memo or '')
        add_field('document_type', document_type)
        if issue_date:
            add_field('issue_date', issue_date)

        # ファイル本体
        body_parts.append(f'--{boundary}\r\n'.encode())
        body_parts.append(f'Content-Disposition: form-data; name="receipt"; filename="{filename}"\r\n'.encode())
        body_parts.append(f'Content-Type: {mime}\r\n\r\n'.encode())
        body_parts.append(file_data)
        body_parts.append(f'\r\n--{boundary}--\r\n'.encode())
        body_bytes = b''.join(body_parts)

        # トークンリフレッシュ
        headers = self._headers('POST')
        headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
        req = urllib.request.Request(
            f"{BASE_URL}/receipts",
            data=body_bytes,
            headers=headers,
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                text = res.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            body_bytes = e.read()
            body_str = body_bytes.decode(errors='replace')
            if e.code == 401:
                self.refresh_token()
                # リトライ (簡略化・1回のみ)
                headers = self._headers('POST')
                headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
                req2 = urllib.request.Request(f"{BASE_URL}/receipts", data=body_bytes,
                                              headers=headers, method='POST')
                with urllib.request.urlopen(req2, timeout=60) as res:
                    return json.loads(res.read().decode())
            raise FreeeError(e.code, body_str)

    def delete_receipt(self, receipt_id):
        """DELETE /receipts/{id}"""
        return self._request('DELETE', f'/receipts/{receipt_id}?company_id={self.company_id}')

    def attach_receipt_to_deal(self, deal_id, receipt_id):
        """
        PUT /deals/{deal_id} で receipts フィールドに receipt_id を追加.
        既存 receipts を保持して追加するのが望ましいが、簡略化して置換.
        """
        deal = self.get_deal(deal_id)['deal']
        receipt_ids = [r.get('id') for r in deal.get('receipts', []) if r.get('id')]
        if receipt_id not in receipt_ids:
            receipt_ids.append(receipt_id)
        body = {
            'company_id': self.company_id,
            'issue_date': deal.get('issue_date'),
            'type': deal.get('type'),
            'receipt_ids': receipt_ids,
        }
        return self._request('PUT', f'/deals/{deal_id}', body=body)

    # === 固定資産関連 (R15・2026-07-11 追加) ===
    def get_fixed_asset_categories(self):
        """GET /fixed_asset_categories - 固定資産カテゴリ (耐用年数マスタ) 一覧"""
        p = {'company_id': self.company_id}
        return self._request('GET', '/fixed_asset_categories', params=p)

    def get_fixed_assets(self, offset=0, limit=100):
        """GET /fixed_assets - 固定資産台帳 一覧"""
        p = {'company_id': self.company_id, 'offset': offset, 'limit': limit}
        return self._request('GET', '/fixed_assets', params=p)

    def post_fixed_asset(self, body):
        """POST /fixed_assets - 固定資産台帳に登録"""
        return self._request('POST', '/fixed_assets', body=body)

    def get_trial_bs(self, fiscal_year=None):
        """GET /reports/trial_bs - 試算表 (貸借対照表)

        2026-08-11 追加: 案Y 段階1「口座残高マイナス」検知の SSoT。
        Japan Made屋 会計期は 6月始まり (2026-06-01〜2027-05-31 = fiscal_year=2026)。
        """
        p = {'company_id': self.company_id}
        if fiscal_year is not None:
            p['fiscal_year'] = fiscal_year
        return self._request('GET', '/reports/trial_bs', params=p)

    def get_negative_balances(self, fiscal_year=None,
                              target_categories=None):
        """試算表から マイナス残高 の勘定科目を抽出.

        2026-08-11 追加: freee UI「口座残高のマイナス＜現金＞」等の警告と対応.
        当期純損益金額・剰余金 等の「正常な赤字」(自己資本マイナス系) は除外し、
        「現金・預金」カテゴリ配下のみを対象とするのが default.

        Args:
            fiscal_year: 会計期 (デフォルト=今日基準で自動算出)
            target_categories: 対象 account_category_name の set
                              (デフォルト = {'現金・預金'})

        Returns:
            list of dict [{account_item_name, account_category_name, closing_balance}, ...]
        """
        if target_categories is None:
            target_categories = {'現金・預金'}
        if fiscal_year is None:
            # Japan Made屋 会計期 = 6月始まり (5月末〆)
            from datetime import date
            today = date.today()
            fiscal_year = today.year if today.month >= 6 else today.year - 1
        r = self.get_trial_bs(fiscal_year=fiscal_year)
        bs = r.get('trial_bs', {})
        balances = bs.get('balances', [])
        result = []
        for b in balances:
            cat = b.get('account_category_name') or ''
            if cat not in target_categories:
                continue
            bal = b.get('closing_balance') or 0
            if bal < 0:
                result.append({
                    'account_item_name': b.get('account_item_name'),
                    'account_category_name': cat,
                    'closing_balance': bal,
                    'account_item_id': b.get('account_item_id'),
                })
        return result


# === CLI test ===
if __name__ == '__main__':
    import sys
    c = FreeeClient()
    if len(sys.argv) > 1 and sys.argv[1] == 'refresh':
        tok = c.refresh_token()
        print(f"refreshed: expires_in={tok.get('expires_in')}s")
    elif len(sys.argv) > 1 and sys.argv[1] == 'ping':
        body = c.get_user_matchers(limit=1)
        print(f"user_matchers OK: {len(body.get('data', []))}件 サンプル")
    else:
        print("Usage: lib_freee.py [refresh|ping]")
