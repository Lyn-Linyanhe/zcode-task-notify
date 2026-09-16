"""长任务心跳：回合运行超过间隔时，推送"仍在运行"提醒。

用法: python heartbeat.py <session_id> [--turn-id T] [--interval-sec N] [--max-minutes N]
由 heartbeat_spawn.py 在 UserPromptSubmit hook 中以分离进程方式启动。

盯的是 payload 里的 turnId 那一行，而不是"该会话最新一行"——turn_usage 的行是回合
**结束后**才整体写入的，所以：
  - 行不存在   = 回合还在跑（此时用心跳自己的启动时刻当起点估算）
  - 行存在且终态 = 回合已结束（结果由 Stop hook 的 push_worker 推送，心跳静默退出）
旧实现按"最新一行"判断，拿到的永远是上一个已结束的回合，于是每次启动都秒退：
2026-09-14 实测 52 次启动、45 次"turn finished"秒退、推送 ⏳ 共 0 次——长任务提醒从未生效。

异常中止兜底：Stop hook 在进程死亡（桌面端关闭/崩溃/强杀）时不会触发，本心跳是
唯一能发现"任务没跑完就没了"的组件——回合未结束而桌面端进程消失时，推 🛑 告知
（复核一次排除瞬时抖动）再退出。

退出条件：回合结束（Stop hook 负责推送结果）/ 桌面端进程消失（先推 🛑 再退出）/
达到时长上限。
"""
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from notify import (load_json, get_session_title, send_notification,  # noqa: E402
                    notifications_enabled, CONFIG_PATH, STATE_PATH, SESSION_DB, BOT_STATE)

HB_LOG = os.path.join(BASE, "heartbeat_log.jsonl")
TERMINAL = ("completed", "error", "cancelled")
TICK_SEC = 10  # 判定节拍：回合一结束就能在一个节拍内退出，不用干等整个间隔


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


def turn_row(turn_id):
    """该回合的行：返回 (status, started_at, completed_at) 或 None（= 还没落库）。"""
    try:
        con = sqlite3.connect(SESSION_DB, timeout=3)
        row = con.execute(
            "SELECT status, started_at, completed_at FROM turn_usage WHERE turn_id=?",
            (turn_id,)).fetchone()
        con.close()
        return row
    except Exception:
        return None


def turn_progress(turn_id, spawned_at, now):
    """→ (finished, elapsed_min)。行未落库即"仍在运行"，用 spawned_at 当起点估算。"""
    row = turn_row(turn_id)
    if row is None:
        return False, max(1, int((now - spawned_at) / 60))
    status, started_at, completed_at = row
    if status in TERMINAL or completed_at:
        return True, None
    base = (started_at / 1000) if started_at else spawned_at
    return False, max(1, int((now - base) / 60))


def is_bot_session(session_id):
    state = load_json(BOT_STATE, {})
    bots = state.get("bots")
    items = bots.values() if isinstance(bots, dict) else (bots or [])
    return any(b.get("activeTaskId") == session_id
               for b in items if isinstance(b, dict))


def _lock_path(session_id):
    import hashlib
    return os.path.join(BASE, f"hb_lock_{hashlib.md5(session_id.encode()).hexdigest()[:12]}.lock")


def _pid_alive(pid):
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not h:
        return False
    k32.CloseHandle(h)
    return True


def acquire_lock(session_id, max_age_s, turn_id=""):
    """同会话单实例锁：已有活着的同会话心跳 → False（防多条「⏳」重复推送）。
    锁的持有进程已死或超时（> 1.5x 最大跟踪时长）则接管。
    锁里额外存 session_id/turn_id：微信「在跑」指令靠它列出运行中的会话
    （锁文件名是 session_id 的哈希，不可逆，所以必须写进内容）。"""
    path = _lock_path(session_id)
    existing = load_json(path, {})
    pid = existing.get("pid")
    started = existing.get("started", 0)
    if pid and _pid_alive(int(pid)) and time.time() - started < max_age_s * 1.5:
        return False
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "started": time.time(),
                   "session_id": session_id, "turn_id": turn_id}, f)
    return True


def _arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else ""


def main():
    session_id = sys.argv[1] if len(sys.argv) > 1 else ""
    turn_id = _arg("--turn-id")
    interval_sec = int(_arg("--interval-sec")) if _arg("--interval-sec") else None
    max_minutes = int(_arg("--max-minutes")) if _arg("--max-minutes") else None

    config = load_json(CONFIG_PATH, {})
    interval_sec = interval_sec or int(config.get("heartbeat_interval_min", 30)) * 60
    max_minutes = max_minutes or int(config.get("heartbeat_max_hours", 4)) * 60

    if not session_id or is_bot_session(session_id):
        return 0
    state = load_json(STATE_PATH, {"enabled": True})
    if not notifications_enabled(state)[0]:
        return 0
    webhook = config.get("webhook")
    if not webhook and not os.path.exists(os.path.join(BASE, "aibot_config.json")):
        return 0  # 两个通道都没有
    if not turn_id:
        # 没有回合号就无从判断"这一轮是否还在跑"（看最新一行只会看到上一轮），宁可不推
        hb_log({"ts": time.time(), "exit": "no turn id in payload", "session_id": session_id})
        return 0

    if not acquire_lock(session_id, max_minutes, turn_id):
        hb_log({"ts": time.time(), "exit": "another heartbeat running", "session_id": session_id})
        return 0

    spawned_at = time.time()
    next_push_at = spawned_at + interval_sec
    hb_log({"ts": spawned_at, "start": session_id, "turn_id": turn_id[:20],
            "interval_sec": interval_sec})

    while True:
        time.sleep(TICK_SEC)
        now = time.time()
        # 顺序关键：回合结束/超限的检查必须在桌面端存活检查**之前**——
        # 否则"任务刚完成 + 用户立刻关桌面端"会被误报成异常中止
        finished, elapsed_min = turn_progress(turn_id, spawned_at, now)
        if finished:
            hb_log({"ts": now, "exit": "turn finished", "session_id": session_id,
                    "turn_id": turn_id[:20]})
            return 0  # 结果由 Stop hook 推送
        # 防御：finished=False 时 elapsed 理论上是数字；万一异常返回 None（改代码引入的
        # 隐式契约破裂），绝不能让 `>=` 抛错崩掉心跳进程（那样提醒会永久失效且无日志）。
        if elapsed_min is None:
            hb_log({"ts": now, "error": "elapsed_min is None with finished=False",
                    "session_id": session_id})
            return 0
        if elapsed_min >= max_minutes:
            hb_log({"ts": now, "exit": "max duration", "session_id": session_id,
                    "elapsed_min": elapsed_min})
            return 0
        if not zcode_alive():
            # 回合未结束而桌面端进程消失 = 会话异常中止（正常结束的话上面已退出）。
            # Stop hook 不会在进程死亡时触发，这里就是唯一的通知机会。
            # 复核一次排除瞬时抖动（如桌面端快速重启）；确认消失才推送。
            time.sleep(15)
            if zcode_alive():
                hb_log({"ts": time.time(), "note": "desktop transient, keep tracking",
                        "session_id": session_id})
                continue
            title = f"🛑 桌面端已关闭，会话随之中止｜{get_session_title(session_id)}"
            summary = (f"任务未完成（中止时已运行 {elapsed_min} 分钟），"
                       f"重新打开后需重新交代任务。\n"
                       f"> {time.strftime('%H:%M')} · {session_id[:16]}")
            try:
                ok, channel, _ = send_notification(webhook, title, summary)
                hb_log({"ts": time.time(), "notified": ok, "channel": channel,
                        "exit": "desktop closed", "elapsed_min": elapsed_min,
                        "session_id": session_id})
            except Exception as e:
                hb_log({"ts": time.time(), "error": str(e)[:150],
                        "exit": "desktop closed", "session_id": session_id})
            return 0
        if now >= next_push_at:
            title = f"⏳ 任务仍在运行（已 {elapsed_min} 分钟）｜{get_session_title(session_id)}"
            summary = f"长任务尚未结束，不需要操作。\n> {time.strftime('%H:%M')} · {session_id[:16]}"
            try:
                ok, channel, _ = send_notification(webhook, title, summary)
                hb_log({"ts": now, "pushed": ok, "channel": channel,
                        "elapsed_min": elapsed_min, "session_id": session_id})
            except Exception as e:
                hb_log({"ts": now, "error": str(e)[:150], "session_id": session_id})
            next_push_at = now + interval_sec


if __name__ == "__main__":
    sys.exit(main())
