"""UserPromptSubmit hook：读取 payload → 校验 → 分离进程启动 heartbeat.py。

顺带兼任 UserPromptSubmit payload 探针（前 20 次落盘 usp_probe.jsonl）。
"""
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from notify import (load_json, log, bot_session_ids,  # noqa: E402
                    STATE_PATH, BOT_STATE)

PROBE_PATH = os.path.join(BASE, "usp_probe.jsonl")
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {"_raw": raw[:1000] if 'raw' in dir() else None}

    # 探针：捕获 payload 结构（保留最近 20 条）
    try:
        lines = []
        if os.path.exists(PROBE_PATH):
            with open(PROBE_PATH, encoding="utf-8") as f:
                lines = f.read().splitlines()
        lines.append(json.dumps(payload, ensure_ascii=False))
        with open(PROBE_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[-20:]) + "\n")
    except Exception:
        pass

    session_id = payload.get("session_id") or payload.get("sessionId") or ""

    # 过滤：开关 / bot 会话 / 子代理
    state = load_json(STATE_PATH, {"enabled": True})
    if not state.get("enabled", True):
        return 0
    bots = load_json(BOT_STATE, {}).get("bots")
    items = bots.values() if isinstance(bots, dict) else (bots or [])
    if any(b.get("activeTaskId") == session_id for b in items if isinstance(b, dict)):
        return 0
    if session_id.startswith("sess_subagent"):
        return 0

    # 分离进程启动心跳
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(BASE, "heartbeat.py"), session_id],
            creationflags=DETACHED, cwd=BASE,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True)
    except Exception as e:
        log({"ts": time.time(), "error": f"heartbeat spawn: {e}", "session_id": session_id})
    return 0


if __name__ == "__main__":
    sys.exit(main())
