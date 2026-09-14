"""推送 worker：由 notify.py 分离启动，完成全部耗时逻辑。

流程：读 payload 文件 → 过滤（bot/子代理/开关二次确认）→ 查 turn 真实状态
     → 摘要（可选 LLM 增强，失败自动回退启发式）→ 企业微信推送（带重试）→ 清理。
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from notify import (load_json, log, make_summary, send_notification,  # noqa: E402
                    get_session_title, notifications_enabled,
                    CONFIG_PATH, STATE_PATH, SESSION_DB, MAX_SUMMARY)

SUMMARY_SYSTEM = ("把 AI 助手的任务回复压缩成一条微信通知摘要：一句话说清做了什么和结果，"
                  "60字以内，直接给内容，不要客套和markdown，保留关键数字。"
                  "如果回复是创作内容（小说/文案等），给出作品标题和一句话内容亮点。")


def bot_session_ids():
    bots = load_json(os.path.expanduser("~/.zcode/v2/bot-state.v2.json"), {}).get("bots")
    items = bots.values() if isinstance(bots, dict) else (bots or [])
    return {b["activeTaskId"] for b in items if isinstance(b, dict) and b.get("activeTaskId")}


def _query_turn(turn_id):
    try:
        con = sqlite3.connect(SESSION_DB, timeout=3)
        row = con.execute("SELECT status, error_code, duration_ms FROM turn_usage WHERE turn_id=?",
                          (turn_id,)).fetchone()
        con.close()
        return (row[0], row[1], row[2]) if row else (None, None, None)
    except Exception:
        return None, None, None


TERMINAL = ("completed", "error", "cancelled")


def turn_status(turn_id, wait_s=0):
    """查回合状态，必要时等待它落库。

    Stop hook 触发时 turn_usage 里通常**还没有本回合的行**：那行是回合彻底结束后才整体
    写入的（含 duration_ms / error_code）。所以除了等 running 变终态，更要等"行出现"。
    只判 `status == "running"` 会在行缺失（None）时一次都不等就返回，导致耗时显示、
    出错识别、取消跳过全部失效——2026-09-14 实测 46 条推送无一带耗时、无一条 ❌。
    """
    if not turn_id:
        return None, None, None
    deadline = time.time() + max(0.0, float(wait_s))
    status, err, dur = _query_turn(turn_id)
    while status not in TERMINAL and time.time() < deadline:
        time.sleep(0.5)
        status, err, dur = _query_turn(turn_id)
    return status, err, dur


def format_duration(ms):
    """turn_usage.duration_ms → 「23 分钟」样式的文本；缺失或几秒的回合返回空（不值得标）。"""
    try:
        s = int(ms) // 1000
    except (TypeError, ValueError):
        return ""
    if s < 60:
        return f"{s} 秒" if s >= 10 else ""
    if s < 3600:
        return f"{round(s / 60)} 分钟"
    h, rem = divmod(s, 3600)
    m = round(rem / 60)
    return f"{h} 小时" + (f" {m} 分钟" if m else "")


def _plan_credentials():
    """动态读取桌面端 start-plan 凭证（JWT 随桌面端自动刷新，读最新的）。"""
    try:
        prov = load_json(os.path.expanduser("~/.zcode/v2/config.json"), {}).get("provider", {})
        opts = (prov.get("builtin:bigmodel-start-plan") or {}).get("options") or {}
        return opts.get("apiKey"), opts.get("baseURL")
    except Exception:
        return None, None


def llm_summary(text, cfg):
    """LLM 摘要。支持 anthropic（start-plan 通道，默认零配置）与 openai 两种格式。
    未启用/凭证缺失/任何失败 → 返回 None（调用方回退启发式）。"""
    if not cfg.get("use_llm_summary"):
        return None
    text = (text or "")[:2000]
    if not text:
        return None

    fmt = cfg.get("llm_api_format") or "anthropic"
    key, base, model = cfg.get("llm_api_key"), cfg.get("llm_base_url"), cfg.get("llm_model")
    if fmt == "anthropic" and (not key or not base):
        key, base = _plan_credentials()
        base = (base or "https://zcode.z.ai/api/v1/zcode-plan/anthropic").rstrip("/")
        model = model or "glm-5.3-flash"
    if not key:
        return None

    # 摘要只影响通知的"可读性"，不该拖慢通知本身：超时默认 10 秒（旧值 25 秒，
    # 实测 39 次尝试里 9 次跑满 25 秒读超时后仍回退启发式——白等 25 秒）。
    timeout_s = float(cfg.get("llm_timeout_sec", 10))
    t0 = time.time()
    try:
        if fmt == "anthropic":
            url = base + "/v1/messages"
            body = {"model": model, "max_tokens": 150, "system": SUMMARY_SYSTEM,
                    "messages": [{"role": "user", "content": text}]}
            headers = {"Content-Type": "application/json", "x-api-key": key,
                       "Authorization": "Bearer " + key, "anthropic-version": "2023-06-01"}
        else:
            url = base.rstrip("/") + "/chat/completions"
            body = {"model": model, "max_tokens": 300,
                    "messages": [{"role": "user",
                                  "content": SUMMARY_SYSTEM + "\n\n回复内容：\n" + text}],
                    "thinking": {"type": "disabled"}}
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
        resp = json.loads(urllib.request.urlopen(
            urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers),
            timeout=timeout_s).read())
        if fmt == "anthropic":
            out = "".join(c.get("text", "") for c in resp.get("content", []) if c.get("type") == "text")
        else:
            msg = resp["choices"][0]["message"]
            out = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
        log({"ts": time.time(), "llm_ms": int((time.time() - t0) * 1000)})
        return out[:MAX_SUMMARY] or None
    except Exception as e:
        log({"ts": time.time(), "llm_error": str(e)[:150],
             "llm_ms": int((time.time() - t0) * 1000)})
        return None


def process_payload(payload):
    """决策 + 组装。返回 {'action':'skip','reason'} 或 {'action':'push','title','body','event','session_id'}。"""
    event = payload.get("hookEventName") or payload.get("hook_event_name") or "?"
    session_id = payload.get("session_id") or payload.get("sessionId") or "?"

    state = load_json(STATE_PATH, {"enabled": True})
    ok, reason = notifications_enabled(state)
    if not ok:
        return {"action": "skip", "reason": reason, "session_id": session_id}
    config = load_json(CONFIG_PATH, {})
    webhook = config.get("webhook")
    has_aibot = os.path.exists(os.path.join(BASE, "aibot_config.json"))
    if not webhook and not has_aibot:
        return {"action": "skip", "reason": "no channel", "session_id": session_id}
    if event == "ConfigSelfHeal":
        keys = "、".join(payload.get("removed_keys") or [])
        return {"action": "push", "title": "🔧 hooks 配置自愈",
                "body": f"检测到 config.json 顶层出现毒化键（{keys}），已自动备份并移除，通知功能恢复——对之后新开的会话生效。",
                "event": event, "session_id": session_id}
    if session_id in bot_session_ids():
        return {"action": "skip", "reason": "bot session", "session_id": session_id}
    if session_id.startswith("sess_subagent"):
        return {"action": "skip", "reason": "subagent", "session_id": session_id}

    if event == "PermissionRequest":
        title, body = "⏸️ 任务等待你的确认", "ZCode 需要你批准一个操作，回电脑或微信里处理。"
    elif event == "Stop":
        turn_id = payload.get("turnId") or payload.get("turn_id")
        t0 = time.time()
        status, _err, dur_ms = turn_status(turn_id, wait_s=config.get("stop_status_wait_sec", 60))
        # 诊断：这条能区分「行还没落库」和「真没耗时」，排查耗时/❌ 缺失时先看它
        log({"ts": time.time(), "turn_status": status, "duration_ms": dur_ms,
             "waited_s": round(time.time() - t0, 1), "session_id": session_id})
        if status == "cancelled":
            return {"action": "skip", "reason": "turn cancelled by user", "session_id": session_id}
        title = "❌ 任务出错" if status == "error" else "✅ 任务完成"
        dur = format_duration(dur_ms)
        if dur:
            title += f"（耗时 {dur}）"
        body = make_summary(payload.get("responsePreview") or payload.get("responseText") or "")
        if not body:
            body = f"回合已结束（{status or '状态未知'}）。"
    else:
        title, body = f"🔔 {event}", json.dumps(payload, ensure_ascii=False)[:150]

    return {"action": "push", "title": f"{title}｜{get_session_title(session_id)}",
            "body": body, "event": event, "session_id": session_id}


def main():
    if len(sys.argv) < 2:
        return 0
    payload_path = sys.argv[1]
    payload = load_json(payload_path, {})
    try:
        os.remove(payload_path)
    except Exception:
        pass
    if not payload:
        return 0

    result = process_payload(payload)
    if result["action"] == "skip":
        log({"ts": time.time(), "skipped": result["reason"], "session_id": result.get("session_id")})
        return 0

    config = load_json(CONFIG_PATH, {})
    body = result["body"]
    if result["event"] == "Stop":
        llm = llm_summary(payload.get("responsePreview") or payload.get("responseText") or "", config)
        if llm:
            body = llm
            log({"ts": time.time(), "llm_summary_used": True, "session_id": result["session_id"]})

    final = f"{body}\n> {time.strftime('%H:%M')} · {result['session_id'][:16]}"
    ok, channel, detail = send_notification(config.get("webhook"), result["title"], final)
    log({"ts": time.time(), "pushed": ok, "channel": channel, "detail": detail[:200],
         "session_id": result["session_id"], "title": result["title"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
