"""
consecutive_failure_helper.py

通知の本質改修 (絶対恒久・2026-09-05 たくぞう明示確立):
- 個別 run 失敗は silent (state 記録のみ)
- N 回連続失敗で初通知
- run 成功でカウント完全リセット + 復旧通知
- 時間ベース判定は禁止 (回数ベース一択)
- 初通知後は 30分 cool-down 継続適用

通知先送り (絶対恒久・2026-09-18 たくぞう明示追加・§ 通知は最低限 実装レベル):
- N >= 2 スキル: N 到達時点は pending_notify=True で silent・N+1 到達 (未復旧継続) で初通知
- N == 1 スキル (発火1回 daily/weekly/monthly): 現行通り 1回失敗で即通知 (検知遅延回避)
- pending 段階での success は全 silent (無駄通知ゼロ化)
- 一過性 N 回失敗+自動復旧パターンを構造的に silent 化

参照:
- CLAUDE.md § 常駐スキル通知は「連続失敗 N 回」で初発火・個別 run 失敗は silent (2026-09-05)
- CLAUDE.md § 通知は最低限 (2026-09-16) / § 通知設計の第一原則 (2026-09-14)
- memory feedback_notification_essence_final_failure_only.md
- vault philosophy_notification_final_failure_only_20260905.md

使い方 (統合 API・run 終了時に 1 回呼ぶだけ):

    from consecutive_failure_helper import handle_run_result

    # run 成功時
    handle_run_result(
        skill_name="inventory-roji-sync",
        stage="run_sync",
        success=True,
        state_dir="./state",
        chatwork_token=os.environ["CHATWORK_API_TOKEN"],
        chatwork_room_id=218962687,
    )

    # run 失敗時
    handle_run_result(
        skill_name="inventory-roji-sync",
        stage="run_sync",
        success=False,
        error_msg=traceback.format_exc(),
        threshold_N=6,  # スキル別 (発火1回=1・10分間隔=6・15分間隔=4・30分間隔=2)
        state_dir="./state",
        chatwork_token=os.environ["CHATWORK_API_TOKEN"],
        chatwork_room_id=218962687,
    )
"""

import datetime
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

STATE_FILENAME = "consecutive_failure.json"
COOLDOWN_MINUTES = 30  # § 失敗通知の cool-down (2026-07-23) 継続適用


def _now_jst() -> str:
    """現在時刻を JST ISO 形式で返す"""
    tz = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(tz).isoformat()


def _parse_iso(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s)


def _load_state(state_dir: Path) -> dict:
    state_file = state_dir / STATE_FILENAME
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / STATE_FILENAME
    state_file.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _get_entry(state: dict, skill_name: str, stage: str) -> dict:
    """state から skill/stage エントリを取得 (なければ初期化)"""
    entry = state.setdefault(skill_name, {}).setdefault(stage, {
        "consecutive_count": 0,
        "first_failure_at": None,
        "last_failure_at": None,
        "last_error_msg": None,
        "threshold_N": None,
        "notified": False,
        "notify_at": None,
        "cooldown_until": None,
        "recovered_at": None,
        "pending_notify": False,
        "pending_notify_at": None,
    })
    # 既存 state に pending_notify が無い場合は後方互換で追加
    entry.setdefault("pending_notify", False)
    entry.setdefault("pending_notify_at", None)
    return entry


def record_failure(
    skill_name: str,
    stage: str,
    error_msg: str,
    threshold_N: int,
    state_dir: Path,
) -> Tuple[bool, str]:
    """
    失敗を記録する。N 回連続到達で should_notify=True を返す。

    Returns:
        (should_notify, reason)
        should_notify=True なら Chatwork 通知すべき
    """
    state = _load_state(state_dir)
    entry = _get_entry(state, skill_name, stage)

    now = _now_jst()
    entry["consecutive_count"] += 1
    entry["last_failure_at"] = now
    entry["last_error_msg"] = (error_msg or "")[:500]
    entry["threshold_N"] = threshold_N
    entry["recovered_at"] = None

    if entry["first_failure_at"] is None:
        entry["first_failure_at"] = now

    count = entry["consecutive_count"]

    if count < threshold_N:
        should_notify = False
        reason = f"連続失敗 {count}/{threshold_N} (閾値未達・silent)"
    elif count == threshold_N:
        # 通知先送りロジック (2026-09-18):
        # - N == 1 スキル (発火1回) は検知遅延回避のため現行通り即通知
        # - N >= 2 スキル (高頻度) は pending_notify=True で silent・次 run 未復旧で昇格通知
        if threshold_N == 1:
            should_notify = True
            reason = f"連続失敗 {count}/{threshold_N} 到達・初通知 (N=1 発火1回スキル)"
            entry["notified"] = True
            entry["notify_at"] = now
            cd_until = _parse_iso(now) + datetime.timedelta(minutes=COOLDOWN_MINUTES)
            entry["cooldown_until"] = cd_until.isoformat()
        else:
            should_notify = False
            reason = f"連続失敗 {count}/{threshold_N} 到達・pending_notify (次 run 未復旧で昇格通知・silent)"
            entry["pending_notify"] = True
            entry["pending_notify_at"] = now
    else:
        # count > threshold_N
        pending = entry.get("pending_notify", False)
        already_notified = entry.get("notified", False)
        now_dt = _parse_iso(now)
        if pending and not already_notified:
            # pending → 昇格通知 (N+1 到達・未復旧確定で初通知)
            should_notify = True
            reason = f"連続失敗 {count}/{threshold_N} 未復旧継続・pending 昇格で初通知"
            entry["notified"] = True
            entry["notify_at"] = now
            entry["pending_notify"] = False
            cd_until = now_dt + datetime.timedelta(minutes=COOLDOWN_MINUTES)
            entry["cooldown_until"] = cd_until.isoformat()
        else:
            cooldown_until = entry.get("cooldown_until")
            if cooldown_until and now_dt < _parse_iso(cooldown_until):
                should_notify = False
                reason = f"連続失敗 {count}/{threshold_N} 継続・cool-down中 (次回可 {cooldown_until})"
            else:
                should_notify = True
                reason = f"連続失敗 {count}/{threshold_N} 継続・cool-down 超過で追加通知"
                entry["notify_at"] = now
                cd_until = now_dt + datetime.timedelta(minutes=COOLDOWN_MINUTES)
                entry["cooldown_until"] = cd_until.isoformat()

    _save_state(state_dir, state)
    return should_notify, reason


def record_success(
    skill_name: str,
    stage: str,
    state_dir: Path,
) -> Tuple[bool, Optional[str], int, Optional[str]]:
    """
    成功を記録する。連続失敗リセット + 復旧通知の判定。

    Returns:
        (should_notify_recovery, recovery_body, prev_count, first_failure_at)
        should_notify_recovery=True なら復旧通知すべき
        (通知が飛んでいた状態からの復旧のみ復旧通知)
    """
    state = _load_state(state_dir)
    entry = _get_entry(state, skill_name, stage)

    was_notified = entry.get("notified", False)
    was_pending = entry.get("pending_notify", False)
    prev_count = entry.get("consecutive_count", 0)
    first_failure_at = entry.get("first_failure_at")

    # 完全リセット
    entry["consecutive_count"] = 0
    entry["first_failure_at"] = None
    entry["last_failure_at"] = None
    entry["last_error_msg"] = None
    entry["notified"] = False
    entry["notify_at"] = None
    entry["cooldown_until"] = None
    entry["pending_notify"] = False
    entry["pending_notify_at"] = None
    entry["recovered_at"] = _now_jst()

    _save_state(state_dir, state)

    # pending 段階での復旧は完全 silent (無駄通知ゼロ化・2026-09-18)
    # 実通知飛んだ状態からの復旧のみ復旧通知
    if was_notified and prev_count > 0:
        body = (
            f"[info][title]✅ {skill_name} 復旧[/title]\n"
            f"stage: {stage}\n"
            f"復旧時刻: {entry['recovered_at']}\n"
            f"連続失敗記録: {prev_count} 回 (初回失敗 {first_failure_at})\n"
            f"→ 現在は正常稼働中[/info]"
        )
        return True, body, prev_count, first_failure_at
    return False, None, prev_count, first_failure_at


def build_failure_notify_body(
    skill_name: str,
    stage: str,
    error_msg: str,
    threshold_N: int,
    state_dir: Path,
    gha_run_url: Optional[str] = None,
) -> str:
    """通知本文を組み立てる。連続失敗回数+初回失敗時刻+エラー要点を含める"""
    state = _load_state(state_dir)
    entry = _get_entry(state, skill_name, stage)
    count = entry.get("consecutive_count", 0)
    first_failure_at = entry.get("first_failure_at", "?")
    last_failure_at = entry.get("last_failure_at", "?")

    err_lines = (error_msg or "").strip().split("\n")
    err_tail = "\n".join(err_lines[-5:]) if len(err_lines) > 5 else (error_msg or "(エラー本文なし)")

    lines = [
        f"[warning][title]🚨 {skill_name} 連続失敗 {count}/{threshold_N} 到達[/title]",
        f"stage: {stage}",
        f"初回失敗: {first_failure_at}",
        f"最終失敗: {last_failure_at}",
        f"連続失敗回数: {count} 回 (閾値 N={threshold_N})",
        "",
        "エラー要点:",
        err_tail,
        "",
    ]
    if gha_run_url:
        lines.append(f"GitHub Actions run: {gha_run_url}")
        lines.append("")
    lines.append("次通知: 復旧時 or 30分 cool-down 超過後の追加失敗時")
    lines.append("詳細=CLAUDE.md § 常駐スキル通知は「連続失敗 N 回」で初発火 (2026-09-05)")
    lines.append("[/warning]")
    return "\n".join(lines)


def _post_chatwork(token: str, room_id: int, body: str) -> Optional[dict]:
    """Chatwork API に POST。失敗時は None を返す (silent fail)"""
    url = f"https://api.chatwork.com/v2/rooms/{room_id}/messages"
    data = urllib.parse.urlencode({
        "body": body,
        "self_unread": 1,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "X-ChatWorkToken": token,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def handle_run_result(
    skill_name: str,
    stage: str,
    success: bool,
    state_dir,
    threshold_N: int = 1,
    error_msg: str = "",
    chatwork_token: Optional[str] = None,
    chatwork_room_id: Optional[int] = None,
    gha_run_url: Optional[str] = None,
) -> dict:
    """
    統合 API: run 結果 (成功/失敗) を判定して、必要なら Chatwork 通知する。

    Args:
        skill_name: スキル名 (state ファイルのキー)
        stage: ステージ名 (state ファイルのサブキー・run_sync/api_call/等)
        success: run が成功したか
        state_dir: state ファイル配置ディレクトリ (str or Path)
        threshold_N: 連続失敗閾値 (スキル別・default=1)
            - 発火1回スキル (daily/weekly/monthly): 1
            - 10分間隔: 6 (60分継続失敗ライン)
            - 15分間隔: 4 (60分継続失敗ライン)
            - 30分間隔: 2 (60分継続失敗ライン)
        error_msg: 失敗時のエラー本文 (traceback.format_exc() 等)
        chatwork_token: Chatwork API トークン (None なら通知しない・state 記録のみ)
        chatwork_room_id: Chatwork ルーム ID (None なら通知しない)
        gha_run_url: GitHub Actions run URL (通知本文に含める)

    Returns:
        {
            "success": bool,
            "notified": bool (通知したか),
            "notify_type": "failure" | "recovery" | None,
            "reason": str (判定理由),
        }
    """
    state_dir = Path(state_dir)
    result = {
        "success": success,
        "notified": False,
        "notify_type": None,
        "reason": "",
    }

    if success:
        should_notify_recovery, recovery_body, prev_count, first_failure_at = record_success(
            skill_name, stage, state_dir,
        )
        result["reason"] = f"success recorded (prev_count={prev_count})"
        if should_notify_recovery and chatwork_token and chatwork_room_id:
            resp = _post_chatwork(chatwork_token, chatwork_room_id, recovery_body)
            if resp:
                result["notified"] = True
                result["notify_type"] = "recovery"
                result["reason"] += " + recovery notified"
    else:
        should_notify, reason = record_failure(
            skill_name=skill_name,
            stage=stage,
            error_msg=error_msg,
            threshold_N=threshold_N,
            state_dir=state_dir,
        )
        result["reason"] = reason
        if should_notify and chatwork_token and chatwork_room_id:
            body = build_failure_notify_body(
                skill_name=skill_name,
                stage=stage,
                error_msg=error_msg,
                threshold_N=threshold_N,
                state_dir=state_dir,
                gha_run_url=gha_run_url,
            )
            resp = _post_chatwork(chatwork_token, chatwork_room_id, body)
            if resp:
                result["notified"] = True
                result["notify_type"] = "failure"
                result["reason"] += " + failure notified"

    return result


def get_state_summary(skill_name: str, stage: str, state_dir) -> dict:
    """現在の state 状態を取得 (デバッグ/確認用)"""
    state = _load_state(Path(state_dir))
    return _get_entry(state, skill_name, stage).copy()


if __name__ == "__main__":
    # 単体テスト
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)

        # ケース1: 発火1回スキル (N=1) → 1回失敗で通知
        print("=== ケース1: N=1 (発火1回スキル) ===")
        r = handle_run_result(
            skill_name="test-daily-skill",
            stage="daily_run",
            success=False,
            error_msg="HTTPError: 500",
            threshold_N=1,
            state_dir=state_dir,
        )
        print(f"  失敗1回目: notified={r['notified']}, reason={r['reason']}")
        assert r["reason"].startswith("連続失敗 1/1 到達"), r

        # ケース2: 高頻度スキル N=6 → 5回失敗まで silent・6回目で通知
        print("\n=== ケース2: N=6 (10分間隔スキル) ===")
        for i in range(1, 8):
            r = handle_run_result(
                skill_name="test-10min-skill",
                stage="run_sync",
                success=False,
                error_msg=f"HTTPError: 500 (attempt {i})",
                threshold_N=6,
                state_dir=state_dir,
            )
            print(f"  失敗{i}回目: notified_would={r['reason']}")

        # ケース3: 途中で成功 → リセット
        print("\n=== ケース3: N=6 で 3回失敗→成功→リセット ===")
        state_dir2 = Path(tempfile.mkdtemp())
        for i in range(3):
            handle_run_result(
                skill_name="test-reset",
                stage="run",
                success=False,
                error_msg=f"err {i}",
                threshold_N=6,
                state_dir=state_dir2,
            )
        summary = get_state_summary("test-reset", "run", state_dir2)
        print(f"  失敗3回後: count={summary['consecutive_count']}")
        assert summary["consecutive_count"] == 3
        r = handle_run_result(
            skill_name="test-reset",
            stage="run",
            success=True,
            state_dir=state_dir2,
        )
        summary = get_state_summary("test-reset", "run", state_dir2)
        print(f"  成功1回後: count={summary['consecutive_count']}, notified={r['notified']}")
        assert summary["consecutive_count"] == 0
        assert r["notified"] is False  # まだ通知飛んでない状態からの復旧 → 復旧通知なし

        # ケース4: 通知飛んだ後の復旧
        print("\n=== ケース4: N=1 で 1回失敗→通知飛ぶ→成功→復旧通知 ===")
        state_dir3 = Path(tempfile.mkdtemp())
        # 失敗
        r = handle_run_result(
            skill_name="test-recovery",
            stage="run",
            success=False,
            error_msg="err",
            threshold_N=1,
            state_dir=state_dir3,
        )
        summary = get_state_summary("test-recovery", "run", state_dir3)
        print(f"  失敗1回後: count={summary['consecutive_count']}, notified={summary['notified']}")
        assert summary["notified"] is True
        # 成功
        should_notify_rec, rec_body, prev_count, _ = record_success(
            "test-recovery", "run", state_dir3,
        )
        print(f"  成功後: should_notify_recovery={should_notify_rec}, prev_count={prev_count}")
        assert should_notify_rec is True
        assert prev_count == 1

        # ケース5 (2026-09-18): N=2 pending_notify 先送り + 一過性復旧で silent
        print("\n=== ケース5: N=2 で 2回失敗→pending→成功→完全silent (無駄通知ゼロ) ===")
        state_dir5 = Path(tempfile.mkdtemp())
        # 1回目失敗
        r1 = handle_run_result(
            skill_name="test-pending-silent",
            stage="run",
            success=False,
            error_msg="transient err 1",
            threshold_N=2,
            state_dir=state_dir5,
        )
        print(f"  失敗1回目: notified={r1['notified']}, reason={r1['reason']}")
        assert r1["notified"] is False
        # 2回目失敗 (N 到達) → pending・通知しない
        r2 = handle_run_result(
            skill_name="test-pending-silent",
            stage="run",
            success=False,
            error_msg="transient err 2",
            threshold_N=2,
            state_dir=state_dir5,
        )
        print(f"  失敗2回目 (N到達): notified={r2['notified']}, reason={r2['reason']}")
        assert r2["notified"] is False, f"N到達で即通知は禁止: {r2}"
        summary = get_state_summary("test-pending-silent", "run", state_dir5)
        assert summary["pending_notify"] is True, f"pending_notify=True 期待: {summary}"
        assert summary["notified"] is False, f"notified=False 期待: {summary}"
        # 3回目成功 → 完全 silent (pending 破棄・復旧通知なし)
        r3 = handle_run_result(
            skill_name="test-pending-silent",
            stage="run",
            success=True,
            state_dir=state_dir5,
        )
        print(f"  成功3回目: notified={r3['notified']}, reason={r3['reason']}")
        assert r3["notified"] is False, f"pending段階からの復旧は silent 必須: {r3}"
        summary = get_state_summary("test-pending-silent", "run", state_dir5)
        assert summary["pending_notify"] is False
        assert summary["consecutive_count"] == 0

        # ケース6 (2026-09-18): N=2 で 3回連続失敗 → 3回目 (N+1) で昇格通知
        print("\n=== ケース6: N=2 で 3回連続失敗 → 3回目で昇格通知 ===")
        state_dir6 = Path(tempfile.mkdtemp())
        r1 = handle_run_result(
            skill_name="test-escalate",
            stage="run",
            success=False,
            error_msg="err1",
            threshold_N=2,
            state_dir=state_dir6,
        )
        print(f"  失敗1回目: notified={r1['notified']}")
        assert r1["notified"] is False
        r2 = handle_run_result(
            skill_name="test-escalate",
            stage="run",
            success=False,
            error_msg="err2",
            threshold_N=2,
            state_dir=state_dir6,
        )
        print(f"  失敗2回目 (pending): notified={r2['notified']}, reason={r2['reason']}")
        assert r2["notified"] is False
        summary = get_state_summary("test-escalate", "run", state_dir6)
        assert summary["pending_notify"] is True
        # 3回目失敗 → 昇格判定 (chatwork_token=None なので実送信はしないが should_notify=True になる)
        should_notify, reason = record_failure(
            skill_name="test-escalate",
            stage="run",
            error_msg="err3",
            threshold_N=2,
            state_dir=state_dir6,
        )
        print(f"  失敗3回目 (N+1・未復旧確定): should_notify={should_notify}, reason={reason}")
        assert should_notify is True, f"3回目で昇格通知必須: {reason}"
        summary = get_state_summary("test-escalate", "run", state_dir6)
        assert summary["notified"] is True
        assert summary["pending_notify"] is False

        # ケース7 (2026-09-18): N=1 スキル (発火1回) は現行通り即通知維持
        print("\n=== ケース7: N=1 (発火1回スキル) は現行通り即通知 ===")
        state_dir7 = Path(tempfile.mkdtemp())
        should_notify, reason = record_failure(
            skill_name="test-n1-immediate",
            stage="run",
            error_msg="err",
            threshold_N=1,
            state_dir=state_dir7,
        )
        print(f"  失敗1回目 (N=1): should_notify={should_notify}, reason={reason}")
        assert should_notify is True, f"N=1 は即通知必須: {reason}"
        summary = get_state_summary("test-n1-immediate", "run", state_dir7)
        assert summary["notified"] is True
        assert summary["pending_notify"] is False, f"N=1 で pending は禁止: {summary}"

        print("\n✅ 全7ケース合格 (2026-09-18 案B pending_notify 先送りロジック含む)")
