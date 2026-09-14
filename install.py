#!/usr/bin/env python3
"""ZCode Task Notify 一键安装。

- 生成 scripts/config.json（从 config.example.json，可 --webhook 直接填企业微信机器人地址）
- 生成 scripts/state.json（开关，默认开）
- 将三个 hook（Stop / PermissionRequest / UserPromptSubmit）安全合并进
  %USERPROFILE%\\.zcode\\cli\\config.json 的 hooks 键（自动备份，只动 hooks 键）

用法:
  python install.py                                   # 交互式：按提示粘贴 webhook
  python install.py --webhook "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
  python install.py --llm-key "xxx.yyy"               # 顺带开启 LLM 摘要
  python install.py --aibot --bot-id "aib..." --bot-secret "..."   # 手机批准/拒绝
  python install.py --target "D:\\somewhere"           # 指定 .zcode 根目录（默认用户主目录）
  python install.py --uninstall                       # 卸载（保留其他配置）
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, "scripts")
EXAMPLE = os.path.join(HERE, "config.example.json")

HOOK_EVENTS = ["Stop", "PermissionRequest", "UserPromptSubmit"]
MIN_PY = (3, 10)
WEBHOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
WEBHOOK_HINT = ("  获取路径：手机企业微信 → 群 → 右上角「···」→「消息推送」→ 添加自定义消息推送\n"
                "  形如 " + WEBHOOK_PREFIX + "xxxxxxxx-xxxx-xxxx")


def looks_like_webhook(url):
    """群机器人 webhook 的形态校验——防止把 Bot ID、群名或别的地址粘进来后一路静默失败。"""
    u = (url or "").strip()
    return u.startswith(WEBHOOK_PREFIX) and len(u) > len(WEBHOOK_PREFIX) + 8


def group_hook_args(group):
    return [str(a) for h in ((group or {}).get("hooks") or [])
            for a in (((h or {}).get("args")) or [])]


def is_our_group(group, scripts_dir):
    """识别本工具的 hook 条目：① 当前这份；② 本工具的另一份拷贝（换过目录、重新下载过）。

    ② 必须也认出来——否则重装后会变成两条 hook 同时跑：一条推送正常、另一条静默失败，
    现象是"通知时有时无 / 有重复失败日志"，排查起来很费劲（2026-09-14 实测踩到）。
    判据：参数文件名是本工具的入口，且同目录下还有 push_worker.py 与 heartbeat_spawn.py。
    """
    current = {os.path.normcase(os.path.join(scripts_dir, n))
               for n in ("notify.py", "heartbeat_spawn.py")}
    for a in group_hook_args(group):
        if not a:
            continue
        if os.path.normcase(a) in current:
            return True
        d = os.path.dirname(a)
        if (os.path.basename(a).lower() in ("notify.py", "heartbeat_spawn.py")
                and os.path.exists(os.path.join(d, "push_worker.py"))
                and os.path.exists(os.path.join(d, "heartbeat_spawn.py"))):
            return True
    return False


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def uninstall(target):
    zcode_cfg = os.path.join(target, ".zcode", "cli", "config.json")
    existing = read_json(zcode_cfg, None)
    if existing is None:
        print(f"[warn] {zcode_cfg} 不存在，无需卸载")
        return 0
    hooks = existing.get("hooks") or {}
    events = hooks.get("events") or {}
    removed = 0
    for event in list(events):
        groups = events[event]
        kept = [g for g in groups if not is_our_group(g, SCRIPTS_DIR)]
        removed += len(groups) - len(kept)
        if kept:
            events[event] = kept
        else:
            del events[event]
    if removed:
        if not events:
            hooks.pop("events", None)
            hooks["enabled"] = False
        backup = zcode_cfg + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(zcode_cfg, backup)
        write_json(zcode_cfg, existing)
        print(f"[ok] 已移除 {removed} 个本工具的 hook 条目（原配置备份: {backup}）")
        print("[note] scripts/config.json 含 webhook 凭证，确认不再使用可手动删除")
    else:
        print("[ok] 没有找到本工具注册的 hooks")
    return 0


def install_aibot(args):
    """--aibot：创建 SDK 虚拟环境 + 写 aibot_config.json（手机批准/拒绝功能）。"""
    import subprocess
    import venv as venv_mod
    venv_dir = os.path.join(HERE, ".venv-aibot")
    venv_py = os.path.join(venv_dir, "Scripts", "python.exe")
    if not os.path.exists(venv_py):
        print("[..] 创建 SDK 虚拟环境 .venv-aibot ...")
        venv_mod.create(venv_dir, with_pip=True)
    # 锁版本：SDK 的 reply / reply_stream 语义是本工具正确性的前提（40008 事故的根因），
    # 官方发新版可能改动签名或消息类型约束——不要放开成无版本约束。
    SDK_PIN = "wecom-aibot-python-sdk==1.0.2"
    print(f"[..] 安装 {SDK_PIN}（需联网）...")
    r = subprocess.run([venv_py, "-m", "pip", "install", "--quiet", SDK_PIN],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[FAIL] SDK 安装失败: {r.stderr[-300:]}")
        return 1
    cfg_path = os.path.join(SCRIPTS_DIR, "aibot_config.json")
    cfg = read_json(cfg_path, {})
    if args.bot_id:
        cfg["bot_id"] = args.bot_id
    if args.bot_secret:
        cfg["secret"] = args.bot_secret
    if not cfg.get("bot_id") or not cfg.get("secret"):
        print("[FAIL] 缺 Bot ID / Secret。在企业微信 机器人详情→API设置 复制后重跑：")
        print('       python install.py --aibot --bot-id "..." --bot-secret "..."')
        return 1
    cfg.setdefault("target_userid", "")
    cfg.setdefault("local_port", 17899)
    cfg.setdefault("card_timeout_sec", 100)
    cfg["venv_python"] = venv_py
    write_json(cfg_path, cfg)
    print(f"[ok] 已生成 {cfg_path}（Secret 只存本地，.gitignore 已排除）")
    print("[note] 给机器人发一条单聊消息，连接器会自动捕获推送地址并就绪")
    return 0


def main():
    if sys.version_info < MIN_PY:
        print(f"[FAIL] 需要 Python {MIN_PY[0]}.{MIN_PY[1]} 或更高，当前是 {sys.version.split()[0]}")
        print("       下载 https://www.python.org/downloads/ —— 安装时务必勾选「Add Python to PATH」")
        return 1

    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default="", help="企业微信群机器人 webhook 地址")
    ap.add_argument("--target", default=os.path.expanduser("~"), help=".zcode 所在根目录（默认用户主目录）")
    ap.add_argument("--test", action="store_true", help="安装后发一条测试消息（默认填了 webhook 就发）")
    ap.add_argument("--no-test", action="store_true", help="装完不发测试消息")
    ap.add_argument("--llm-key", default="", help="顺带开启 LLM 摘要：填 bigmodel API Key（形如 xxx.yyy）")
    ap.add_argument("--uninstall", action="store_true", help="移除本工具注册的 hooks（保留其他配置）")
    ap.add_argument("--aibot", action="store_true", help="安装「手机批准/拒绝」组件（智能机器人长连接）")
    ap.add_argument("--bot-id", default="", help="智能机器人 Bot ID（--aibot 用）")
    ap.add_argument("--bot-secret", default="", help="智能机器人 Secret（--aibot 用）")
    args = ap.parse_args()

    if args.uninstall:
        return uninstall(args.target)

    if args.aibot:
        rc = install_aibot(args)
        if rc != 0:
            return rc

    # 1) 生成 scripts/config.json
    cfg_path = os.path.join(SCRIPTS_DIR, "config.json")
    if os.path.exists(cfg_path):
        print(f"[skip] {cfg_path} 已存在，不覆盖")
    else:
        cfg = read_json(EXAMPLE, {})
        webhook = args.webhook.strip()
        if webhook and not looks_like_webhook(webhook):
            print("[warn] --webhook 看着不像群机器人地址，仍然写入，但很可能收不到消息：")
            print(WEBHOOK_HINT)
        for _ in range(3):
            if webhook:
                break
            try:
                webhook = input("粘贴企业微信群机器人 webhook 地址（回车跳过、稍后手填）: ").strip()
            except EOFError:
                break
            if webhook and not looks_like_webhook(webhook):
                print("[warn] 这不像群机器人 webhook（多半少了 https:// 前缀或粘错了东西）：")
                print(WEBHOOK_HINT)
                webhook = ""
        if webhook:
            cfg["webhook"] = webhook
        write_json(cfg_path, cfg)
        print(f"[ok] 已生成 {cfg_path}" + ("" if cfg.get("webhook") else " —— 注意：webhook 还没填！"))

    # 1b) 可选：顺带开启 LLM 摘要（省得用户手改 JSON）
    if args.llm_key:
        cfg = read_json(cfg_path, {})
        cfg["use_llm_summary"] = True
        cfg["llm_api_key"] = args.llm_key.strip()
        cfg.setdefault("llm_api_format", "openai")
        cfg.setdefault("llm_base_url", "https://open.bigmodel.cn/api/paas/v4")
        cfg.setdefault("llm_model", "glm-4.5-flash")
        write_json(cfg_path, cfg)
        print("[ok] 已开启 LLM 摘要（模型 " + str(cfg.get("llm_model")) + "）")

    # 2) 生成 state.json（开关，默认开）
    state_path = os.path.join(SCRIPTS_DIR, "state.json")
    if not os.path.exists(state_path):
        write_json(state_path, {"enabled": True, "updated": time.strftime("%Y-%m-%d")})
        print(f"[ok] 已生成 {state_path}（推送默认开启）")

    # 3) 合并 hooks 到 ZCode 用户级配置
    zcode_cli = os.path.join(args.target, ".zcode", "cli")
    zcode_cfg = os.path.join(zcode_cli, "config.json")
    existing = read_json(zcode_cfg, None)
    if existing is None and not os.path.exists(zcode_cfg):
        existing = {}
        print(f"[warn] {zcode_cfg} 不存在，将新建（正常：从未改过配置的机器上可能没有此文件）")

    backup = zcode_cfg + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    if os.path.exists(zcode_cfg):
        shutil.copy2(zcode_cfg, backup)
        print(f"[ok] 原配置已备份: {backup}")

    hooks = existing.get("hooks") or {}
    hooks["enabled"] = True
    events = hooks.get("events") or {}
    current_norm = {os.path.normcase(os.path.join(SCRIPTS_DIR, n))
                    for n in ("notify.py", "heartbeat_spawn.py")}
    replaced = set()

    for event in HOOK_EVENTS:
        entry_script = "heartbeat_spawn.py" if event == "UserPromptSubmit" else "notify.py"
        kept = []
        for g in (events.get(event) or []):
            if is_our_group(g, SCRIPTS_DIR):
                for a in group_hook_args(g):
                    if a and os.path.normcase(a) not in current_norm:
                        replaced.add(os.path.dirname(a))
                continue
            kept.append(g)
        # 注意：ZCode 的 events schema 是 [{matcher?, hooks:[定义]}] 的嵌套结构（strict 校验），
        # 不是扁平的 hook 定义数组——写错会被静默拒绝。
        kept.append({
            "hooks": [{
                "type": "process",
                "command": sys.executable or "python",
                "args": [os.path.join(SCRIPTS_DIR, entry_script)],
                # PermissionRequest 给手机批准留 120s 决策窗口（连接器不在线时流程秒退，不受影响）
                "timeoutMs": 120000 if event == "PermissionRequest" else 5000,
            }]
        })
        events[event] = kept
    hooks["events"] = events
    existing["hooks"] = hooks
    write_json(zcode_cfg, existing)
    print(f"[ok] hooks 已写入 {zcode_cfg}（事件: {', '.join(HOOK_EVENTS)}；已有其他 hook 保留不动）")
    for stale in sorted(replaced):
        print(f"[note] 已清掉指向旧位置的重复注册: {stale}")
        print("       （换目录/重新下载后重跑本脚本即可，不用手动改配置）")

    # 4) 测试推送：填了 webhook 就默认发一条——让用户当场在手机上看到"通了"，
    #    而不是等到下一个任务结束才发现地址填错（--no-test 可关）
    cfg_now = read_json(cfg_path, {})
    webhook_now = str(cfg_now.get("webhook", "")).strip()
    if not args.no_test and webhook_now:
        sys.path.insert(0, SCRIPTS_DIR)
        try:
            from notify import send_wecom_once
            ok, detail = send_wecom_once(webhook_now, "🩺 安装成功",
                                         "zcode-task-notify 已安装，任务通知将推送到这个群。")
            if ok:
                print("[ok] 测试消息已发出——看一眼手机上的企业微信群，收到就说明通道通了")
            else:
                print(f"[FAIL] 测试消息没发出去: {detail}")
                print("       常见原因：webhook 地址粘贴不全 / 群机器人已被移除 / 网络不通")
        except Exception as e:
            print(f"[FAIL] 测试推送异常: {e}")
    elif not webhook_now and not args.uninstall:
        print("[warn] 还没填 webhook —— 装是装上了，但通知发不出去。填好后重跑本脚本即可。")
        print(WEBHOOK_HINT)

    print("""
安装完成。接下来：
1. 手机上确认已收到「🩺 安装成功」——没收到就先跑 python doctor.py 定位问题
2. 在 ZCode 里**新建一个会话**（hooks 只对新会话生效），随便问一句
3. 会话结束时企业微信应收到「✅ 任务完成（耗时 xx）」
4. 想临时关推送：直接给机器人发「静默」，或改 scripts/state.json 的 enabled
5. 出问题：python doctor.py（16 项自检）；想卸载：python install.py --uninstall
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
