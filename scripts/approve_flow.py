"""PermissionRequest 阻塞决策流：手机卡片批准/拒绝，超时回退桌面确认。

在 notify.py 的 hook 进程内执行，须在 hook 的 timeoutMs（120s）内完成：
1. 组卡片内容（会话/工具/输入预览）
2. 连接器在线 → POST /card 推权限卡片到手机；不在线 → 返回 fallback（降级 webhook 通知）
3. 轮询 decisions/<task_id>.json（1s 间隔，至多 card_timeout_sec 秒）
4. 拿到决定返回 "allow"/"deny"；超时返回 None（无 stdout，ZCode 自动回退桌面 UI）
仅标准库。
"""
import json
import os
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
TASK_PREFIX = "zc-"


def _cfg():
    return json.load(open(os.path.join(BASE, "aibot_config.json"), encoding="utf-8"))


def _input_preview(tool_input):
    try:
        s = tool_input if isinstance(tool_input, str) else json.dumps(
            tool_input, ensure_ascii=False)
    except Exception:
        s = str(tool_input)
    return s[:300]


def _post_card(port, body, timeout=5):
    headers = {"Content-Type": "application/json"}
    token = _cfg().get("local_token") or ""   # 连接器启动时生成的本地共享密钥
    if token:
        headers["X-Zcn-Token"] = token
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/card",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")).get("ok") is True


def _health(port, timeout=2):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok")) and bool(data.get("connected"))
    except Exception:
        return False


def _decision_file(task_id):
    return os.path.join(BASE, "decisions", task_id + ".json")


def _user_active(threshold_s=60):
    """人在电脑前吗：键鼠最后输入距今 < threshold 秒即视为在位。
    在位时跳过手机卡片流——桌面确认弹窗即时出现，不必等手机。"""
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return False  # 查询失败 → 按不在电脑前处理
        idle_ms = (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
        return idle_ms < threshold_s * 1000
    except Exception:
        return False  # 异常 → 按不在电脑前处理（保持卡片流可用）


def _ask_spec(payload):
    """AskUserQuestion payload → 多选卡规格；None = 不适合（走批准/拒绝或交桌面）。

    实测（pr_probe.jsonl，2026-09-15）：AskUserQuestion 会触发 PermissionRequest，
    tool_input.questions[].options[] 带 label/description；工具的 answers 字段
    （键=问题文本，值=选项 label）由"permission component"收集——我们就是那个组件。
    只接单问题且非 multiSelect；多问题/多选交回桌面 UI。
    """
    tool = payload.get("tool_name") or payload.get("toolName") or ""
    if tool != "AskUserQuestion":
        return None
    ti = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(ti, dict):
        return None
    qs = ti.get("questions") or []
    if len(qs) != 1:
        return None
    q = qs[0] or {}
    opts = q.get("options") or []
    if not (2 <= len(opts) <= 5) or q.get("multiSelect"):
        return None
    question = str(q.get("question") or "")
    if not question:
        return None
    return {"question": question,
            "options": [{"key": "ABCDE"[i], "label": str(o.get("label") or f"选项{i+1}")}
                        for i, o in enumerate(opts)],
            "tool_input": ti}


def run(payload):
    """返回 decision dict / None（超时，交桌面 UI） / "fallback"（连接器不可用或人在电脑前）。

    二选一卡 → {"behavior": "allow"/"deny"}；
    AskUserQuestion 多选卡 → 用户点选项后 {"behavior": "allow",
    "updatedInput": {原 tool_input + answers}}——ZCode 按 modify 放行，
    agent 直接拿到答案继续跑（实测协议："Allowed with modified input"）。
    """
    try:
        cfg = _cfg()
    except Exception:
        return "fallback"
    if _user_active(int(cfg.get("desk_active_sec", 60))):
        return "fallback"
    request_id = payload.get("requestId") or payload.get("request_id") \
        or f"{int(time.time())}-{os.getpid()}"
    task_id = TASK_PREFIX + str(request_id)[:60]
    port = int(cfg.get("local_port", 17899))

    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    tool = payload.get("tool_name") or payload.get("toolName") or "?"
    reason = payload.get("reason") or ""
    try:
        from notify import get_session_title
        title = get_session_title(session_id) if session_id else ""
    except Exception:
        title = ""

    spec = _ask_spec(payload)
    if spec:
        # 会话名进标题：微信通知预览/聊天列表只显示标题行，放正文里等于没有
        card_title = f"{title} · 请选择" if title else "ZCode 请选择"
        mapping = "　".join(f"{o['key']}={o['label']}" for o in spec["options"])
        desc = f"{spec['question'][:200]}\n{mapping}"
        body = {"task_id": task_id, "title": card_title, "desc": desc,
                "options": [{"key": o["key"], "text": o["key"], "label": o["label"]}
                            for o in spec["options"]]}
    else:
        card_title = f"{title} · 请求确认" if title else "ZCode 请求确认"
        desc = f"工具：{tool}\n输入：{_input_preview(payload.get('tool_input') or payload.get('toolInput'))}"
        if reason:
            desc += f"\n原因：{reason}"
        body = {"task_id": task_id, "title": card_title, "desc": desc}

    if not _health(port):
        return "fallback"
    try:
        if not _post_card(port, body):
            return "fallback"
    except Exception:
        return "fallback"

    deadline = time.time() + int(cfg.get("card_timeout_sec", 100))
    while time.time() < deadline:
        try:
            with open(_decision_file(task_id), encoding="utf-8") as f:
                data = json.load(f)
            if time.time() - data.get("ts", 0) < 600:
                if spec and "choice" in data:
                    # 用户点选：把选项写进 answers（键=问题文本，值=选项 label），放行
                    ti = dict(spec["tool_input"])
                    ti["answers"] = {spec["question"]: data["choice"]}
                    return {"behavior": "allow", "updatedInput": ti}
                d = data.get("decision")
                if d == "allow":
                    return {"behavior": "allow"}
                if d == "deny":
                    return {"behavior": "deny", "message": "已在手机上拒绝（zcode通知）"}
        except Exception:
            pass
        time.sleep(1)
    return None
