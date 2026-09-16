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
from notify import (load_json, log, bot_session_ids, notifications_enabled,  # noqa: E402
                    STATE_PATH, BOT_STATE)

PROBE_PATH = os.path.join(BASE, "usp_probe.jsonl")
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
    try:
        import urllib.request
        data = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=1.5).read().decode("utf-8"))
        if data.get("connected") and data.get("target"):
            return  # 连接器在线且已认证绑定
        need_restart = True  # HTTP 活着但 ws 掉线/未绑定 → 需要重启
    except Exception:
        need_restart = True  # 端口不通（未运行/启动中）
    venv = cfg.get("venv_python")
    if not venv or not os.path.exists(venv):
        return  # 未安装 SDK 环境（install --aibot 才会有）
    if need_restart:
        _kill_stale_connector(port)
    try:
        subprocess.Popen(
            [venv, os.path.join(BASE, "aibot_connector.py")],
            creationflags=NOWINDOW, cwd=BASE,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True)
        log({"ts": time.time(), "connector_spawn": True,
             "reason": "ws offline" if need_restart else "port down"})
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
    if session_id.startswith("sess_subagent"):
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
