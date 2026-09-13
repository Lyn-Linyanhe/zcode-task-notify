"""ZCode Task Notify 自检：一条命令定位"收不到通知"的问题。

用法: python doctor.py [--no-send] [--target <.zcode根目录>]
默认会向 webhook 发一条测试消息验证通道（--no-send 跳过）。
每项检查输出 PASS / WARN / FAIL；有 FAIL 时退出码 1。
"""
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from notify import load_json, send_wecom_once  # noqa: E402

# 兼容两种目录布局：仓库布局（scripts/ 子目录）与生产实例布局（扁平）
_CANDIDATE_DIRS = [os.path.join(BASE, "scripts"), BASE, os.path.expanduser("~/.zcode/task-notify")]


def find_file(name):
    for d in _CANDIDATE_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def find_config():
    p = find_file("config.json")
    if p:
        cfg = load_json(p, {})
        if cfg.get("webhook"):
            return cfg, p
    return load_json(p or "", {}) if p else {}, p

results = []


def check(name, ok, hint="", warn=False):
    status = ("PASS" if ok else ("WARN" if warn else "FAIL"))
    results.append((name, status, hint))
    print(f"[{status}] {name}" + (f" —— {hint}" if hint and not ok else ""))
    return ok


def main():
    send_test = "--no-send" not in sys.argv
    target = None
    if "--target" in sys.argv:
        target = sys.argv[sys.argv.index("--target") + 1]

    print("ZCode Task Notify 自检\n" + "=" * 40)

    # 1. Python 版本
    v = sys.version_info
    check("Python >= 3.10", (v.major, v.minor) >= (3, 10),
          f"当前 {v.major}.{v.minor}，请升级", warn=(v.major, v.minor) >= (3, 8))

    # 2. 平台
    check("运行平台 Windows", sys.platform == "win32",
          "当前脚本仅支持 Windows（Win32 API）", warn=True)

    # 3. ZCode 运行中
    try:
        from heartbeat import zcode_alive
        zc = zcode_alive()
    except Exception:
        zc = None
    check("ZCode 桌面端运行中", bool(zc), "桌面端没开——通知链路整体下线", warn=(zc is None))

    # 4. 开关
    state = load_json(find_file("state.json") or "", {})
    check("推送开关已开启", bool(state.get("enabled", True)),
          "state.json 的 enabled 为 false")

    # 5. webhook 配置
    cfg, cfg_path = find_config()
    webhook = (cfg.get("webhook") or "").strip()
    check("webhook 已配置", webhook.startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=")
          and len(webhook) > 60, f"config（{cfg_path or '未找到'}）缺 webhook 或格式不对")

    # 6. hooks 注册
    zcfg = load_json(zcode_cfg_path(target), {})
    hooks = zcfg.get("hooks") or {}
    events = hooks.get("events") or {}
    check("hooks.enabled = true", hooks.get("enabled") is True,
          f"{zcode_cfg_path(target)} 里 hooks.enabled 不是 true")

    def event_ok(event, script):
        groups = events.get(event) or []
        return any(
            any(str(a).endswith(script) for a in ((h or {}).get("args") or []))
            for g in groups for h in (g.get("hooks") or []))

    check("Stop hook 已注册", event_ok("Stop", "notify.py"),
          f"{zcode_cfg_path(target)} 的 events.Stop 里找不到指向 notify.py 的条目（是否开新会话过？）")
    check("PermissionRequest hook 已注册", event_ok("PermissionRequest", "notify.py"),
          "同上")
    check("UserPromptSubmit hook 已注册", event_ok("UserPromptSubmit", "heartbeat_spawn.py"),
          "events.UserPromptSubmit 里找不到 heartbeat_spawn.py")
    check("脚本文件存在", bool(find_file("notify.py")),
          "找不到 notify.py（检查目录布局）")

    # 6.5 顶层键毒化检测：provider 键会让整个 hooks 静默失效（2026-09-13/14 两次实测）
    check("config 顶层键无已知毒键", "provider" not in zcfg,
          "顶层出现 provider 键——hooks 会被整体静默弃用（新会话的心跳 hook 会自动备份并移除；也可手动删除）")
    unknown = [k for k in zcfg if k not in ("plugins", "hooks")]
    if unknown:
        check("config 未知顶层键", True, f"{unknown}（非已知毒键，仅提示关注）", warn=True)

    # 7. webhook 通道实测
    if webhook and send_test:
        try:
            ok, detail = send_wecom_once(webhook, "🩺 自检测试消息",
                                         "收到这条说明企业微信推送通道正常。")
            check("企业微信通道实测", ok, f"推送失败：{detail[:120]}"
                  "（检查网络、webhook key 是否被重置）")
        except Exception as e:
            check("企业微信通道实测", False, f"{type(e).__name__}: {e}")
    elif not send_test:
        check("企业微信通道实测", True, "已跳过（--no-send）", warn=True)

    # 8. LLM（可选）
    if cfg.get("use_llm_summary"):
        has_key = bool(cfg.get("llm_api_key"))
        check("LLM 摘要已启用且 key 已填", has_key, "use_llm_summary=true 但 llm_api_key 为空")
    else:
        check("LLM 摘要", True, "未启用（使用启发式摘要）", warn=True)

    # 9. aibot 手机批准（可选）
    acfg = load_json(find_file("aibot_config.json") or "", {})
    if acfg.get("bot_id"):
        check("aibot 凭据（bot_id/secret）", bool(acfg.get("secret")),
              "aibot_config.json 缺 secret")
        venv_py = acfg.get("venv_python") or ""
        sdk_ok = bool(venv_py) and os.path.exists(venv_py)
        check("aibot SDK 环境", sdk_ok, "未找到 venv 解释器，重跑 python install.py --aibot")
        if sdk_ok:
            online = False
            try:
                import urllib.request
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{int(acfg.get('local_port', 17899))}/health",
                        timeout=2) as resp:
                    online = bool(json.loads(resp.read().decode()).get("connected"))
            except Exception:
                pass
            check("aibot 连接器在线", online,
                  "未在线（下次发消息时看门狗自动拉起；也可手动用 venv python 跑 aibot_connector.py）",
                  warn=True)
    else:
        check("aibot 手机批准", True, "未启用（可选功能，见 README「手机批准/拒绝」）", warn=True)

    # 汇总
    fails = [r for r in results if r[1] == "FAIL"]
    warns = [r for r in results if r[1] == "WARN"]
    print("=" * 40)
    print(f"结果: {len(results) - len(fails) - len(warns)} PASS / {len(warns)} WARN / {len(fails)} FAIL")
    if fails:
        print("有未通过的检查项——按上面 FAIL 行的提示处理，或把本输出发给 AI 助手分析。")
    return 1 if fails else 0


def zcode_cfg_path(target):
    import os
    root = target or os.path.expanduser("~")
    return os.path.join(root, ".zcode", "cli", "config.json")


if __name__ == "__main__":
    sys.exit(main())
