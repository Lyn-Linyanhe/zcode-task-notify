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
MAX_MUTE_MINUTES = 24 * 60

_FILLER = re.compile(r"^(好的?|明白了?|收到|没问题|当然|ok|首先|嗯+|对[，,]?|好的呢)[，,！!。.\s]")
_MUTE_RE = re.compile(r"^静默\s*(\d+)\s*(?:分钟|分|min|m)?$")


def notifications_enabled(state, now=None):
    """是否应当推送 → (bool, 原因)。定时静默（mute_until）由读者按时钟判断，
    不需要额外的定时进程：到点自然失效，state.json 无需清理。"""
    now = time.time() if now is None else now
    if not state.get("enabled", True):
        return False, "switch off"
    if (state.get("mute_until") or 0) > now:
        return False, "muted"
    return True, "on"


def mute_remaining_min(state, now=None):
    """定时静默剩余分钟数；已被手动静默或未静默 → None。"""
    now = time.time() if now is None else now
    if not state.get("enabled", True):
        return None
    left = (state.get("mute_until") or 0) - now
    return max(1, int((left + 59) // 60)) if left > 0 else None


def parse_mute_minutes(text):
    """指令文本 → 静默分钟数。0 = 手动静默（不自动恢复）；None = 不是静默指令。
    上限 24 小时，防手误打成天文数字。"""
    t = (text or "").strip()
    if t in ("静默", "关闭通知", "静音"):
        return 0
    m = _MUTE_RE.match(t)
    return min(int(m.group(1)), MAX_MUTE_MINUTES) if m else None


def sender_allowed(uid, owner, chattype):
    """指令只认主人。已绑定 target_userid 时必须与发送者一致；尚未绑定则只接受单聊的
    第一位发送者（群聊无法确认归属，一律先拒，避免被陌生人抢绑或误控）。"""
    if not uid:
        return False
    if owner:
        return uid == owner
    return chattype == "single"


def update_state(**fields):
    """读-改-写 state.json（静默/恢复都走这里，格式统一）。"""
    state = load_json(STATE_PATH, {"enabled": True})
    state.update(fields)
    state["updated"] = time.strftime("%Y-%m-%d %H:%M")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    return state


def mute_for_minutes(minutes, now=None):
    """定时静默：只写 mute_until，**不动 enabled**——若同时把 enabled 置 false，读取方会
    当成手动静默（提醒语与剩余时间都拿不到），到点也不会自动恢复。"""
    now = time.time() if now is None else now
    return update_state(mute_until=now + minutes * 60)


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


def _pid_alive(pid):
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not h:
        return False
    k32.CloseHandle(h)
    return True


def running_sessions(now=None):
    """当前正在运行的会话：读心跳锁 hb_lock_*.lock——锁进程活着 = 该回合还在跑。

    不能用 turn_usage 判"在跑"：那行是回合**结束后**才写入的，跑着的回合在表里根本没有行。
    返回按已运行时长降序的 [{session_id, title, elapsed_min, turn_id}]。
    旧格式锁（无 session_id 字段）跳过——该会话下次提交消息后即恢复可见。
    """
    now = time.time() if now is None else now
    out = []
    try:
        names = [n for n in os.listdir(BASE) if n.startswith("hb_lock_") and n.endswith(".lock")]
    except Exception:
        return out
    for n in names:
        d = load_json(os.path.join(BASE, n), {})
        sid, pid = d.get("session_id") or "", d.get("pid")
        if not sid or not pid:
            continue
        try:
            if not _pid_alive(int(pid)):
                continue
        except Exception:
            continue
        out.append({"session_id": sid, "turn_id": d.get("turn_id", ""),
                    "elapsed_min": max(1, int((now - d.get("started", now)) / 60)),
                    "title": get_session_title(sid)})
    out.sort(key=lambda r: -r["elapsed_min"])
    return out


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
    """机器人通道：连接器 /notify 在线且已捕获目标 → (True, "ok")，否则 (False, 原因)。
    原因会写进降级提示里，方便一眼看出是没配置、没绑用户还是连接器挂了。"""
    try:
        cfg_path = os.path.join(BASE, "aibot_config.json")
        if not os.path.exists(cfg_path):
            return False, "未配置智能机器人"
        cfg = load_json(cfg_path, {})
        if not cfg.get("bot_id"):
            return False, "未配置智能机器人"
        if not cfg.get("target_userid"):
            return False, "未绑定用户（先在微信里给机器人发一条消息）"
        port = int(cfg.get("local_port", 17899))
        body = {"title": title, "content": f"**{title}**\n{summary}"}
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/notify",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            if json.loads(resp.read().decode("utf-8")).get("ok") is True:
                return True, "ok"
            return False, "连接器拒绝了本条推送"
    except Exception:
        return False, "连接器离线"


def send_notification(webhook, title, summary):
    """统一推送入口：智能机器人（单聊）优先，连接器不可用降级群 webhook。
    走降级通道时在正文里显式标注原因——群机器人只能单向通知，回复指令/点按钮都无效，
    不标注的话"机器人挂了"会被误当成"通知正常"。
    返回 (是否成功, 通道名, 详情)。"""
    ok_aibot, reason = _try_aibot(title, summary)
    if ok_aibot:
        return True, "aibot", "ok"
    if webhook:
        note = (f"> ⚠️ **通道降级**：智能机器人不可用（{reason}），本条由群机器人代发。"
                "群机器人只能单向通知，回复指令或点按钮需等智能机器人恢复。")
        ok, detail = send_wecom(webhook, title, f"{summary}\n{note}")
        return ok, "webhook", detail
    log_failure(f"推送失败（无可用通道：aibot 不可用（{reason}）且未配 webhook）| {title}")
    return False, "none", "no channel"


PR_PROBE_PATH = os.path.join(BASE, "pr_probe.jsonl")


def _probe_permission_payload(payload):
    """PermissionRequest payload 探针（本地文件，保留最近 20 条）：卡片"多选"要能落地，
    必须先看清真实 payload 里到底带不带选项、什么形状——AskUserQuestion 是独立工具，
    是否触发 PermissionRequest 尚未实测，不能凭猜设计。绝不抛错、不影响决策主流程。"""
    try:
        lines = []
        if os.path.exists(PR_PROBE_PATH):
            with open(PR_PROBE_PATH, encoding="utf-8") as f:
                lines = f.read().splitlines()
        lines.append(json.dumps(payload, ensure_ascii=False))
        with open(PR_PROBE_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[-20:]) + "\n")
    except Exception:
        pass


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

    # 快速闸门：开关关闭/定时静默就静默退出（对通知和手机决策流一并生效，详细过滤在 worker 里）
    state = load_json(STATE_PATH, {"enabled": True})
    ok, reason = notifications_enabled(state)
    if not ok:
        log({"ts": time.time(), "skipped": reason,
             "session_id": payload.get("session_id", "?")[:20]})
        return 0

    # PermissionRequest：优先手机卡片批准/拒绝（阻塞流，超时或连接器不在线自动降级）；
    # approve_flow 内部有「人在电脑前」检测——刚用过键鼠就不走手机，桌面弹窗即时出现
    if event == "PermissionRequest":
        _probe_permission_payload(payload)
        decision = "fallback"
        try:
            import approve_flow
            decision = approve_flow.run(payload)
        except Exception as e:
            log({"ts": time.time(), "error": f"approve_flow: {e}"})
        if isinstance(decision, dict) and decision.get("behavior") in ("allow", "deny"):
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision}}, ensure_ascii=False))
            log({"ts": time.time(), "approved": decision.get("behavior"),
                 "choice": (decision.get("updatedInput") or {}).get("answers"),
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
            creationflags=0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | NEW_GROUP
            cwd=BASE, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as e:
        log({"ts": time.time(), "error": f"worker spawn: {e}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
