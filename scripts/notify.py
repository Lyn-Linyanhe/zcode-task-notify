"""ZCode 任务通知 hook 入口（v3）：读 stdin → 落盘 payload → 分离进程启动 push_worker。

所有耗时逻辑（DB 查询/LLM 摘要/webhook 推送）都在 push_worker.py 的分离进程里，
本文件必须秒回，不阻塞 agent 回合结束。
共享工具函数也放在这里（heartbeat.py / push_worker.py 共用）。
"""
import json
import os
import re
import sqlite3
import sys
import subprocess
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "notify_log.jsonl")
FAIL_PATH = os.path.join(BASE, "push_failures.log")
PENDING_DIR = os.path.join(BASE, "pending")
BOT_STATE = os.path.expanduser("~/.zcode/v2/bot-state.v2.json")
SESSION_DB = os.path.expanduser("~/.zcode/cli/db/db.sqlite")

MAX_SUMMARY = 120
HTTP_TIMEOUT = 5
MAX_LOG_LINES = 500
KEEP_LOG_LINES = 400

_FILLER = re.compile(r"^(好的?|明白了?|收到|没问题|当然|ok|首先|嗯+|对[，,]?|好的呢)[，,！!。.\s]")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def log(entry):
    """诊断日志（jsonl），带简单轮转。"""
    try:
        lines = []
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, encoding="utf-8") as f:
                lines = f.read().splitlines()
        lines.append(json.dumps(entry, ensure_ascii=False))
        if len(lines) > MAX_LOG_LINES:
            lines = lines[-KEEP_LOG_LINES:]
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def log_failure(message):
    try:
        with open(FAIL_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + message + "\n")
    except Exception:
        pass


def get_session_title(session_id):
    try:
        con = sqlite3.connect(SESSION_DB, timeout=3)
        row = con.execute("SELECT title FROM session WHERE id=?", (session_id,)).fetchone()
        con.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return (session_id or "未知会话")[:20]


def bot_session_ids():
    state = load_json(BOT_STATE, {})
    bots = state.get("bots")
    items = bots.values() if isinstance(bots, dict) else (bots or [])
    return {b["activeTaskId"] for b in items if isinstance(b, dict) and b.get("activeTaskId")}


def _clean_line(s):
    s = re.sub(r"[*#`>]{1,}", "", s)
    return re.sub(r"\s+", " ", s).strip()


def make_summary(text, limit=MAX_SUMMARY):
    """启发式摘要：结论行 > 加粗/标题行 > 末段 > 首段，过滤客套开头。
    短标题行（如小说标题）自动拼接后续第一句实质内容，避免摘要只剩标题。"""
    if not text:
        return ""
    raw_lines = [l.strip() for l in text.split("\n") if l.strip()]
    lines = [_clean_line(l) for l in raw_lines]
    lines = [l for l in lines if l and not _FILLER.match(l)]
    if not lines:
        return ""

    def _take(idx):
        """候选行太短（<15字）时拼接后续第一行实质内容。"""
        cand = lines[idx]
        if len(cand) < 15:
            nxt = next((l for l in lines[idx + 1:] if len(l) >= 15), None)
            if nxt:
                cand = f"{cand}：{nxt[:limit - len(cand) - 1]}"
        return cand[:limit] + ("…" if len(cand) > limit else "")

    cand = next((i for i, l in enumerate(lines) if re.match(r"^(结论|总结|总之|综上|所以|最终|一句话|答案是)", l)), None)
    if cand is not None:
        return _take(cand)
    for i, raw in enumerate(raw_lines):
        if i >= len(lines):
            break
        mm = re.search(r"\*\*(.+?)\*\*", raw)
        if mm and len(_clean_line(mm.group(1))) >= 8:
            return _take(i)
        if raw.startswith("#"):
            return _take(i)
    if len(lines) > 1 and len(lines[-1]) <= limit:
        return _take(len(lines) - 1)
    return _take(0)


def send_wecom_once(webhook, title, summary):
    body = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{summary}"}}
    req = urllib.request.Request(
        webhook, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read())
    return resp.get("errcode") == 0, str(resp)


def send_wecom(webhook, title, summary):
    """带重试：失败 3 秒后重试 1 次；最终失败写 push_failures.log。"""
    detail = ""
    for attempt in range(2):
        try:
            ok, detail = send_wecom_once(webhook, title, summary)
            if ok:
                return True, detail
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
        if attempt < 1:
            time.sleep(3)
    log_failure(f"推送失败（重试2次后）: {detail} | {title}")
    return False, detail


def _try_aibot(title, summary):
    """机器人通道：连接器 /notify 在线且已捕获目标 → True。任何异常静默 False。"""
    try:
        cfg_path = os.path.join(BASE, "aibot_config.json")
        if not os.path.exists(cfg_path):
            return False
        cfg = load_json(cfg_path, {})
        if not cfg.get("bot_id") or not cfg.get("target_userid"):
            return False
        port = int(cfg.get("local_port", 17899))
        body = {"title": title, "content": f"**{title}**\n{summary}"}
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/notify",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def send_notification(webhook, title, summary):
    """统一推送入口：智能机器人（单聊）优先，连接器不可用降级群 webhook。
    返回 (是否成功, 通道名, 详情)。"""
    if _try_aibot(title, summary):
        return True, "aibot", "ok"
    if webhook:
        ok, detail = send_wecom(webhook, title, summary)
        return ok, "webhook", detail
    log_failure(f"推送失败（无可用通道：aibot 不在线且未配 webhook）| {title}")
    return False, "none", "no channel"


def _cleanup_pending(max_age_s=3600):
    """清理 worker 崩溃遗留的 payload 临时文件（>1 小时）。"""
    try:
        now = time.time()
        for name in os.listdir(PENDING_DIR):
            path = os.path.join(PENDING_DIR, name)
            if os.path.isfile(path) and now - os.path.getmtime(path) > max_age_s:
                os.remove(path)
    except Exception:
        pass


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    event = payload.get("hookEventName") or payload.get("hook_event_name") or "?"

    # 快速闸门：开关关闭就静默退出（对通知和手机决策流一并生效，详细过滤在 worker 里）
    state = load_json(STATE_PATH, {"enabled": True})
    if not state.get("enabled", True):
        log({"ts": time.time(), "skipped": "switch off",
             "session_id": payload.get("session_id", "?")[:20]})
        return 0

    # PermissionRequest：优先手机卡片批准/拒绝（阻塞流，超时或连接器不在线自动降级）；
    # approve_flow 内部有「人在电脑前」检测——刚用过键鼠就不走手机，桌面弹窗即时出现
    if event == "PermissionRequest":
        decision = "fallback"
        try:
            import approve_flow
            decision = approve_flow.run(payload)
        except Exception as e:
            log({"ts": time.time(), "error": f"approve_flow: {e}"})
        if decision in ("allow", "deny"):
            decision_obj = {"behavior": decision}
            if decision == "deny":
                decision_obj["message"] = "已在手机上拒绝（zcode通知）"
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision_obj}}, ensure_ascii=False))
            log({"ts": time.time(), "approved": decision,
                 "session_id": str(payload.get("session_id", "?"))[:20]})
            return 0
        if decision is None:
            # 超时未决定：不输出任何内容，ZCode 自动回退桌面 UI 确认
            log({"ts": time.time(), "approved": "timeout",
                 "session_id": str(payload.get("session_id", "?"))[:20]})
            return 0
        # decision == "fallback" → 继续走下方 webhook ⏸️ 通知

    os.makedirs(PENDING_DIR, exist_ok=True)
    _cleanup_pending()
    temp_path = os.path.join(PENDING_DIR, f"payload_{int(time.time()*1000)}_{os.getpid()}.json")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    try:
        subprocess.Popen(
            [sys.executable, os.path.join(BASE, "push_worker.py"), temp_path],
            creationflags=0x00000008 | 0x00000200,  # DETACHED | NEW_GROUP
            cwd=BASE, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as e:
        log({"ts": time.time(), "error": f"worker spawn: {e}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
