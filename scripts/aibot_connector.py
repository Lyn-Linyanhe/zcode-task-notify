"""企业微信智能机器人常驻连接器（唯一持有长连接的进程）。

职责：
1. 维护与腾讯 openws 的 WebSocket 长连接（心跳/断线重连由 SDK 负责）
2. 本地 HTTP 端点 127.0.0.1:<port>/card —— hook 进程把权限请求 POST 进来，
   连接器经 WebSocket 把带「批准/拒绝」按钮的卡片推到你的单聊
3. 手机点按钮 → 点击事件回流 → 卡片原位更新为结果卡（不可重复点击）
   + 写 decisions/<task_id>.json 给等待中的 hook 进程读取

启动方式：由 heartbeat_spawn 检测拉起（单例锁），无需开机自启。
运行解释器必须是 .venv-aibot（SDK 依赖在其中），主通知系统保持零依赖。
"""
import asyncio
import ctypes
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from aiohttp import web  # noqa: E402
from aibot import WSClient, WSClientOptions  # noqa: E402
from notify import (log, load_json, mute_for_minutes, mute_remaining_min,  # noqa: E402
                    notifications_enabled, parse_mute_minutes, sender_allowed, update_state)

CFG = json.load(open(os.path.join(BASE, "aibot_config.json"), encoding="utf-8"))
DECISIONS_DIR = os.path.join(BASE, "decisions")
TASK_PREFIX = "zc-"


def acquire_lock():
    """单实例锁：文件独占创建 + PID 存活校验（防并发双实例互踢长连接）。"""
    path = os.path.join(BASE, "aibot_connector.lock")
    for _ in range(2):
        try:
            with open(path, "x", encoding="utf-8") as f:  # 独占创建，已存在即抛错
                json.dump({"pid": os.getpid(), "started": time.time()}, f)
            return True
        except FileExistsError:
            try:
                with open(path, encoding="utf-8") as f:
                    pid = json.load(f).get("pid")
                if pid:
                    k32 = ctypes.windll.kernel32
                    h = k32.OpenProcess(0x1000, False, int(pid))
                    if h:
                        k32.CloseHandle(h)
                        return False  # 活实例在跑
            except Exception:
                return False  # 锁文件读不了且被占用 → 宁可退出
            try:
                os.remove(path)  # 死实例残留的锁，清掉重试
            except OSError:
                return False
    return False


def cleanup_decisions(max_age_s=900):
    try:
        now = time.time()
        for name in os.listdir(DECISIONS_DIR):
            p = os.path.join(DECISIONS_DIR, name)
            if now - os.path.getmtime(p) > max_age_s:
                os.remove(p)
    except Exception:
        pass


ws = WSClient(WSClientOptions(bot_id=CFG["bot_id"], secret=CFG["secret"]))


def build_card(task_id, title, desc):
    return {"msgtype": "template_card", "template_card": {
        "card_type": "button_interaction",
        "main_title": {"title": f"⏸️ {title}"},
        "sub_title_text": desc,
        "task_id": task_id,
        "button_list": [{"key": "approve", "text": "✅ 批准"},
                        {"key": "deny", "text": "❌ 拒绝"}],
    }}


STATE_PATH = os.path.join(BASE, "state.json")


HELP_TEXT = (
    "📖 可用指令\n"
    "状态 — 通知开关 / 连接器是否在线\n"
    "静默 / 恢复 — 手动开关全部通知\n"
    "静默 30 — 定时静默 30 分钟，到点自动恢复\n"
    "最近 — 看最近 3 条推送\n"
    "帮助 — 这条说明\n"
    "复杂任务请打开手机远程控制页面，这里只做简单指令。")


def _set_enabled(on):
    """开关通知。任意一次手动开关都会清掉定时静默，避免两个开关打架。"""
    update_state(enabled=on, mute_until=0)


def _status_text():
    state = load_json(STATE_PATH, {"enabled": True})
    enabled, reason = notifications_enabled(state)
    left = mute_remaining_min(state)
    if not enabled and reason == "muted":
        head = f"🔇 定时静默中（还有 {left} 分钟，到点自动恢复）"
    else:
        head = "🔔 通知开启中" if enabled else "🔇 通知已静默（发「恢复」开启）"
    return (f"{head}\n"
            f"连接器：{'在线' if ws.is_connected else '掉线'}\n"
            f"可用指令：发「帮助」查看")


def _recent_text(n=3):
    """最近几条推送（读推送日志，只取成功推送的条目）。"""
    entries = []
    try:
        with open(os.path.join(BASE, "notify_log.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("pushed") and d.get("title"):
                    entries.append(d)
    except Exception:
        pass
    if not entries:
        return "🕘 还没有推送记录。"
    out = [f"🕘 最近 {min(n, len(entries))} 条推送"]
    for d in reversed(entries[-n:]):
        out.append(f"{time.strftime('%H:%M', time.localtime(d['ts']))} {d['title']}")
    return "\n".join(out)


def handle_command(content):
    """返回回复文本；None = 非指令（如首次激活消息），不回复。"""
    t = content.strip()
    if t in ("帮助", "指令", "help", "?"):
        return HELP_TEXT
    if t in ("恢复", "开启通知", "取消静默"):
        _set_enabled(True)
        return "🔔 已恢复通知。"
    if t in ("状态", "status"):
        return _status_text()
    if t in ("最近", "最近通知", "推送记录"):
        return _recent_text()
    minutes = parse_mute_minutes(t)
    if minutes is None:
        return None
    if minutes == 0:
        _set_enabled(False)
        return "🔇 已静默——不再推送任何通知（含权限卡片）。发「恢复」重新开启。"
    until = time.strftime("%H:%M", time.localtime(time.time() + minutes * 60))
    mute_for_minutes(minutes)
    return f"🔇 已静默 {minutes} 分钟（{until} 自动恢复）。想提前结束发「恢复」。"


def _capture_target(uid):
    """首次单聊：记下主人 userid（免去手填），同时作为指令鉴权依据。"""
    CFG["target_userid"] = uid
    try:
        path = os.path.join(BASE, "aibot_config.json")
        cfg = json.load(open(path, encoding="utf-8"))
        cfg["target_userid"] = uid
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        log({"ts": time.time(), "target_captured": uid[:10] + "…"})
        print(f"已捕获单聊目标: {uid[:10]}…", flush=True)
    except Exception as e:
        log({"ts": time.time(), "error": f"target capture: {e}"})


@ws.on("message.text")
async def on_text(frame):
    body = frame.get("body") or {}
    content = ((body.get("text") or {}).get("content") or "").strip()
    if not content:
        return
    uid = (body.get("from") or {}).get("userid") or ""
    chattype = body.get("chattype")

    # 先认主人（首条单聊消息建立绑定），再鉴权——指令只认主人，别人发的只记日志不回
    if not CFG.get("target_userid") and chattype == "single" and uid:
        _capture_target(uid)
    if not sender_allowed(uid, CFG.get("target_userid"), chattype):
        log({"ts": time.time(), "aibot_cmd_ignored": f"not owner {uid[:6]}…"})
        return

    reply_text = handle_command(content)
    if not reply_text:
        return
    log({"ts": time.time(), "aibot_cmd": content[:10]})
    try:
        # 长连接模式没有 text 类型：普通（一次性）回复也必须走流式结构——
        # msgtype=stream + finish=true，否则企业微信回执 40008 invalid message type
        # （2026-09-14 实测：「状态」指令能收到但回复被退回）
        await ws.reply_stream(
            frame, f"stream_{int(time.time() * 1000)}_{os.getpid()}",
            reply_text, finish=True)
    except Exception as e:
        log({"ts": time.time(), "error": f"cmd reply: {e}"})


async def h_health(request):
    return web.json_response({"ok": True, "connected": bool(ws.is_connected),
                              "target": bool(CFG.get("target_userid"))})


async def h_notify(request):
    """hook/worker → 连接器：日常通知经机器人单聊推送。body: {title, content}"""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if not ws.is_connected or not CFG.get("target_userid"):
        return web.json_response({"ok": False, "error": "connector unavailable"}, status=503)
    try:
        await ws.send_message(CFG["target_userid"], {
            "msgtype": "markdown",
            "markdown": {"content": str(data.get("content") or data.get("title") or "")},
        })
        return web.json_response({"ok": True})
    except Exception as e:
        log({"ts": time.time(), "error": f"notify send: {e}"})
        return web.json_response({"ok": False, "error": str(e)[:200]}, status=502)


async def h_card(request):
    """hook 进程 → 连接器：发权限卡片。body: {task_id, title, desc}"""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    task_id = str(data.get("task_id") or "")
    if not task_id.startswith(TASK_PREFIX):
        return web.json_response({"ok": False, "error": "task_id must start with " + TASK_PREFIX},
                                 status=400)
    if not ws.is_connected:
        return web.json_response({"ok": False, "error": "ws offline"}, status=503)
    if not CFG.get("target_userid"):
        return web.json_response(
            {"ok": False, "error": "target_userid 未捕获——先给机器人发一条单聊消息"},
            status=503)
    try:
        await ws.send_message(CFG["target_userid"],
                              build_card(task_id, data.get("title") or "ZCode 请求确认",
                                         data.get("desc") or ""))
        log({"ts": time.time(), "card_sent": task_id})
        return web.json_response({"ok": True})
    except Exception as e:
        log({"ts": time.time(), "error": f"card send: {e}"})
        return web.json_response({"ok": False, "error": str(e)[:200]}, status=502)


@ws.on("event.template_card_event")
async def on_card_click(frame):
    try:
        body = frame.get("body") or {}
        ev = (body.get("event") or {}).get("template_card_event") or {}
        key, task_id = ev.get("event_key"), str(ev.get("task_id") or "")
        if not task_id.startswith(TASK_PREFIX):
            return
        decision = {"approve": "allow", "deny": "deny"}.get(key)
        if not decision:
            return
        os.makedirs(DECISIONS_DIR, exist_ok=True)
        decision_path = os.path.join(DECISIONS_DIR, task_id + ".json")
        if os.path.exists(decision_path):
            # 幂等：结果卡仍保留按钮（保持 button_interaction 是绕开 42045 的代价），
            # 因此同一 task_id 可能被重复点击——只认第一次，不重复写盘、不重复更新卡片。
            log({"ts": time.time(), "decision_dup": key, "task_id": task_id})
            return
        with open(decision_path, "w", encoding="utf-8") as f:
            json.dump({"decision": decision, "ts": time.time()}, f)
        label = "✅ 已批准" if decision == "allow" else "❌ 已拒绝"
        try:
            # 官方要求：更新时保持 card_type 不变（button_interaction），
            # 把按钮改为完成态文案；换 text_notice 会报 42045
            await ws.update_template_card(frame, {
                "card_type": "button_interaction",
                "main_title": {"title": f"{label}（手机决定）",
                               "desc": "决定已同步给 ZCode"},
                "button_list": [{"key": key or "done",
                                 "text": label, "style": 1 if decision == "allow" else 2}],
                "task_id": task_id,
            })
        except Exception as e:
            log({"ts": time.time(), "error": f"card update: {e}"})
        log({"ts": time.time(), "decision": decision, "task_id": task_id})
    except Exception as e:
        log({"ts": time.time(), "error": f"card click: {e}"})


async def main():
    os.makedirs(DECISIONS_DIR, exist_ok=True)
    cleanup_decisions()
    await ws.connect()
    app = web.Application()
    app.router.add_get("/health", h_health)
    app.router.add_post("/card", h_card)
    app.router.add_post("/notify", h_notify)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", int(CFG.get("local_port", 17899))).start()
    print(f"连接器在线：ws {'已连接' if ws.is_connected else '未连接'}，"
          f"HTTP 127.0.0.1:{CFG.get('local_port', 17899)}", flush=True)
    while True:
        await asyncio.sleep(300)
        cleanup_decisions()


if __name__ == "__main__":
    if not acquire_lock():
        print("已有连接器实例在运行，退出", flush=True)
        sys.exit(0)
    asyncio.run(main())
