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
from notify import (_atomic_write_json, load_json, log, bot_session_ids,  # noqa: E402
                    notifications_enabled, is_background_session, STATE_PATH, BOT_STATE)

PROBE_PATH = os.path.join(BASE, "usp_probe.jsonl")
RESTART_STATE_NAME = "connector_restart.json"  # 上次重启连接器的时刻（节流依据）


def restart_state_path():
    """节流状态文件路径。按调用时的 BASE 计算——测试会把 BASE 指向临时目录，
    写成模块级常量会让状态落到真实目录、污染后续用例。"""
    return os.path.join(BASE, RESTART_STATE_NAME)
# 子进程创建标志。不能用 DETACHED_PROCESS：.venv 的 python.exe 是启动器存根，它转手
# 拉起真实解释器时不传该标志，于是没有控制台的父进程会让系统新建控制台，Win11 默认
# 终端（Windows Terminal）就弹出一个窗口——标题是存根路径，看着像 bug 报告。
# CREATE_NO_WINDOW 给一个不可见的控制台，实测无窗口（2026-09-14 四组对照实验）。
NOWINDOW = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

# 已知毒键：出现在 cli/config.json 顶层会让整个 hooks 配置静默失效（2026-09-13/14 两次实测）
POISON_CFG = os.path.expanduser("~/.zcode/cli/config.json")
KNOWN_POISON_KEYS = ("provider",)


def self_heal_config(session_id):
    """毒键自愈：发现已知毒键 → 备份 → 摘除 → 经 push_worker 推送告知。
    只摘 blocklist 键；其他未知键仅记日志。读不到配置时绝不动手。"""
    try:
        cfg = load_json(POISON_CFG, None)
        if not isinstance(cfg, dict) or "hooks" not in cfg:
            return
        removed = [k for k in KNOWN_POISON_KEYS if k in cfg]
        if not removed:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(POISON_CFG + f".bak-{stamp}-selfheal", "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        for k in removed:
            cfg.pop(k)
        with open(POISON_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        log({"ts": time.time(), "selfheal": removed, "session_id": session_id})
        pending = os.path.join(BASE, "pending")
        os.makedirs(pending, exist_ok=True)
        pf = os.path.join(pending, f"selfheal-{stamp}.json")
        with open(pf, "w", encoding="utf-8") as f:
            json.dump({"hookEventName": "ConfigSelfHeal", "session_id": "sess_selfheal",
                       "removed_keys": removed, "ts": time.time()}, f, ensure_ascii=False)
        subprocess.Popen(
            [sys.executable, os.path.join(BASE, "push_worker.py"), pf],
            creationflags=NOWINDOW, cwd=BASE,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True)
    except Exception as e:
        log({"ts": time.time(), "error": f"selfheal: {e}", "session_id": session_id})


def ensure_connector():
    """连接器看门狗：本地 /health 不通**或 ws 掉线** → 用 venv 解释器拉起。

    注意必须同时看 `connected` 字段：进程活着、HTTP 端口在响应、但 WebSocket 已断时，
    只判"HTTP 通"会让看门狗永远认为在线、永不重启——现象是推送一直降级到群 webhook、
    卡片/指令全失效，直到手动重启（2026-09-16 实测：断线 40 分钟无自动恢复，
    SDK 自身的重连在"服务器主动断开"场景下不可靠）。"""
    cfg_path = os.path.join(BASE, "aibot_config.json")
    if not os.path.exists(cfg_path):
        return
    cfg = load_json(cfg_path, {})
    port = int(cfg.get("local_port", 17899))
    need_restart = False
    reason = ""
    try:
        import urllib.request
        data = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=1.5).read().decode("utf-8"))
        if data.get("connected") and data.get("target"):
            return  # 连接器在线且已认证绑定
        need_restart, reason = True, "ws offline"  # HTTP 活着但 ws 掉线/未绑定
    except Exception:
        need_restart, reason = True, "port down"  # 端口不通（未运行/启动中）
    venv = cfg.get("venv_python")
    if not venv or not os.path.exists(venv):
        return  # 未安装 SDK 环境（install --aibot 才会有）
    if need_restart:
        # 节流：连接器从拉起→ws 认证完成实测约 10s，这期间 /health 仍报
        # connected=false。若用户连着提交几条 prompt，每条都会「判定掉线 → 杀掉正在
        # 启动的新实例 → 再拉一个」——杀进程要走 PowerShell（秒级），既拖慢提交，又让
        # 连接器永远起不来。故一个宽限窗内只重启一次，窗内只记日志、不动作。
        now = time.time()
        grace = int(load_json(os.path.join(BASE, "config.json"), {})
                    .get("connector_restart_grace_sec", 60))
        last = float(load_json(restart_state_path(), {}).get("last_restart", 0) or 0)
        if now - last < grace:
            log({"ts": now, "connector_restart_throttled": True, "reason": reason,
                 "since_s": round(now - last, 1), "grace_s": grace})
            return
        _atomic_write_json(restart_state_path(), {"last_restart": now})
        _kill_stale_connector(port)
    try:
        subprocess.Popen(
            [venv, os.path.join(BASE, "aibot_connector.py")],
            creationflags=NOWINDOW, cwd=BASE,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True)
        log({"ts": time.time(), "connector_spawn": True, "reason": reason})
    except Exception as e:
        log({"ts": time.time(), "error": f"connector spawn: {e}"})


def _kill_stale_connector(port):
    """结束掉线/假死的旧连接器进程——否则新实例会因单例锁直接退出，
    而旧实例又永远不会自愈（ws 断线不重连）。只杀命令行里含本目录连接器脚本的进程。"""
    try:
        ps = (f"Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
              f"Where-Object {{ $_.CommandLine -like '*aibot_connector.py*' }} | "
              f"Select-Object -ExpandProperty ProcessId")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30, errors="replace")
        pids = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        if pids:
            log({"ts": time.time(), "connector_killed_stale": pids})
            time.sleep(1.5)  # 等端口与锁释放
    except Exception as e:
        log({"ts": time.time(), "error": f"kill stale connector: {e}"})


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {"_raw": raw[:1000] if 'raw' in dir() else None}

    session_id = payload.get("session_id") or payload.get("sessionId") or ""

    self_heal_config(session_id)
    ensure_connector()
    # 连接器也负责手机审批，内部会话仍须维护连接；只跳过进度心跳。
    if is_background_session(session_id):
        return 0

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

    # 过滤：开关（含定时静默）/ bot 会话 / 子代理
    state = load_json(STATE_PATH, {"enabled": True})
    if not notifications_enabled(state)[0]:
        return 0
    bots = load_json(BOT_STATE, {}).get("bots")
    items = bots.values() if isinstance(bots, dict) else (bots or [])
    if any(b.get("activeTaskId") == session_id for b in items if isinstance(b, dict)):
        return 0

    # 分离进程启动心跳
    turn_id = payload.get("turnId") or payload.get("turn_id") or ""
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(BASE, "heartbeat.py"), session_id, "--turn-id", turn_id],
            creationflags=NOWINDOW, cwd=BASE,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True)
    except Exception as e:
        log({"ts": time.time(), "error": f"heartbeat spawn: {e}", "session_id": session_id})
    return 0


if __name__ == "__main__":
    sys.exit(main())
