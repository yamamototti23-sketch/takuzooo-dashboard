# takuzooo-dashboard

Takuzooo（チャンネルID: `UCjSeXbXh2BS-ErZO7a6ienQ`）の YouTube 統計を毎日自動取得して GitHub Pages に表示するダッシュボード。

## 仕組み

- `fetch_youtube.py`: YouTube Data API + Analytics API を叩いて `docs/data.json` を生成
- `docs/index.html`: `data.json` を fetch して Chart.js で描画
- `.github/workflows/update.yml`: 毎日 06:05 JST に自動更新 + 手動 dispatch 対応

## 公開 URL

`https://<OWNER>.github.io/takuzooo-dashboard/`

## ローカルセットアップ（refresh token の再発行が必要な時のみ）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install google-auth-oauthlib
python get_youtube_token.py    # ブラウザで同意 → secrets.env 生成
deactivate
```

生成された `secrets.env` の3つの値を GitHub Secrets (`YT_CLIENT_ID` / `YT_CLIENT_SECRET` / `YT_REFRESH_TOKEN`) に登録すると workflow が動く。

## 注意

- `client_secret.json` と `secrets.env` は `.gitignore` 済み。コミットしない。
- OAuth クライアントは GCP プロジェクト `starlit-booster-494906-a5`（本番ステータス維持）。
