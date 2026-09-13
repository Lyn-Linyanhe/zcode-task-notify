"""长任务心跳：会话的回合运行超过间隔时，推送"仍在运行"提醒。

用法: python heartbeat.py <session_id> [--interval-sec N] [--max-minutes N]
由 heartbeat_spawn.py 在 UserPromptSubmit hook 中以分离进程方式启动。
退出条件：回合结束（Stop hook 负责推送结果）/ 桌面端进程消失 / 达到时长上限。
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from notify import (load_json, get_session_title, send_wecom,  # noqa: E402
                    log, CONFIG_PATH, STATE_PATH, SESSION_DB, BOT_STATE)

HB_LOG = os.path.join(BASE, "heartbeat_log.jsonl")


def hb_log(entry):
    try:
        with open(HB_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def zcode_alive():
    """枚举进程检查 ZCode.exe 是否存活（ctypes，无编码/路径转换坑）。"""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    try:
        arr = (wintypes.DWORD * 4096)()
        cb = wintypes.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(cb)):
            return True  # 枚举失败保守认为活着
        count = cb.value // ctypes.sizeof(wintypes.DWORD)
        for i in range(count):
            pid = arr[i]
            if not pid:
                continue
            h = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not h:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    if buf.value.lower().endswith("zcode.exe"):
                        return True
            finally:
                k32.CloseHandle(h)
        return False
    except Exception:
        return True  # 异常时保守认为活着


def latest_turn(session_id):
    """该会话最新的回合：返回 (turn_id, status, started_at_ms, completed_at_ms) 或 None。"""
    try:
        con = sqlite3.connect(SESSION_DB, timeout=3)
        row = con.execute(
            "SELECT turn_id, status, started_at, completed_at FROM turn_usage "
            "WHERE session_id=? ORDER BY started_at DESC LIMIT 1", (session_id,)).fetchone()
        con.close()
        return row
    except Exception:
        return None


def is_bot_session(session_id):
    state = load_json(BOT_STATE, {})
    bots = state.get("bots")
    items = bots.values() if isinstance(bots, dict) else (bots or [])
    return any(b.get("activeTaskId") == session_id
               for b in items if isinstance(b, dict))


def main():
    session_id = sys.argv[1] if len(sys.argv) > 1 else ""
    interval_sec = int(sys.argv[sys.argv.index("--interval-sec") + 1]) if "--interval-sec" in sys.argv else None
    max_minutes = int(sys.argv[sys.argv.index("--max-minutes") + 1]) if "--max-minutes" in sys.argv else None

    config = load_json(CONFIG_PATH, {})
    interval_sec = interval_sec or int(config.get("heartbeat_interval_min", 30)) * 60
    max_minutes = max_minutes or int(config.get("heartbeat_max_hours", 4)) * 60

    if not session_id or is_bot_session(session_id):
        return 0
    state = load_json(STATE_PATH, {"enabled": True})
    if not state.get("enabled", True):
        return 0
    webhook = config.get("webhook")
    if not webhook:
        return 0

    hb_log({"ts": time.time(), "start": session_id, "interval_sec": interval_sec})
    started = time.time()
    time.sleep(5)  # 等回合记录落库

    while True:
        if not zcode_alive():
            hb_log({"ts": time.time(), "exit": "desktop gone", "session_id": session_id})
            return 0
        row = latest_turn(session_id)
        if row is None:
            hb_log({"ts": time.time(), "exit": "no turn rows", "session_id": session_id})
            return 0
        turn_id, status, started_at, completed_at = row
        if status in ("completed", "error", "cancelled") or completed_at:
            hb_log({"ts": time.time(), "exit": "turn finished",
                    "session_id": session_id, "turn_id": turn_id[:20], "status": status})
            return 0  # 结果由 Stop hook 推送

        elapsed_min = max(1, int((time.time() - started_at / 1000) / 60))
        if elapsed_min >= max_minutes:
            hb_log({"ts": time.time(), "exit": "max duration", "session_id": session_id})
            return 0

        title = f"⏳ 任务仍在运行（已 {elapsed_min} 分钟）｜{get_session_title(session_id)}"
        summary = f"长任务尚未结束，不需要操作。\n> {time.strftime('%H:%M')} · {session_id[:16]}"
        try:
            send_wecom(webhook, title, summary)
            hb_log({"ts": time.time(), "pushed": True, "elapsed_min": elapsed_min,
                    "session_id": session_id})
        except Exception as e:
            hb_log({"ts": time.time(), "error": str(e)[:150], "session_id": session_id})

        time.sleep(interval_sec)


if __name__ == "__main__":
    sys.exit(main())
