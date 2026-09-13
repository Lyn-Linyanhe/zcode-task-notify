#!/usr/bin/env python3
"""ZCode Task Notify 一键安装。

- 生成 scripts/config.json（从 config.example.json，可 --webhook 直接填企业微信机器人地址）
- 生成 scripts/state.json（开关，默认开）
- 将三个 hook（Stop / PermissionRequest / UserPromptSubmit）安全合并进
  %USERPROFILE%\\.zcode\\cli\\config.json 的 hooks 键（自动备份，只动 hooks 键）

用法:
  python install.py --webhook "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
  python install.py --target "D:\\somewhere"        # 指定 .zcode 根目录（默认用户主目录）
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
    our_markers = ("notify.py", "heartbeat_spawn.py")
    removed = 0
    for event in list(events):
        groups = events[event]
        kept = [g for g in groups
                if not any(any(m in str(a) for m in our_markers)
                           for h in (g.get("hooks") or [])
                           for a in ((h or {}).get("args") or []))]
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
    print("[..] 安装 wecom-aibot-python-sdk（需联网）...")
    r = subprocess.run([venv_py, "-m", "pip", "install", "--quiet",
                        "wecom-aibot-python-sdk"], capture_output=True, text=True)
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default="", help="企业微信群机器人 webhook 地址")
    ap.add_argument("--target", default=os.path.expanduser("~"), help=".zcode 所在根目录（默认用户主目录）")
    ap.add_argument("--test", action="store_true", help="安装后向 webhook 发一条测试消息")
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
        if args.webhook:
            cfg["webhook"] = args.webhook
        elif not cfg.get("webhook", "").startswith("https://qyapi"):
            try:
                args.webhook = input("粘贴企业微信群机器人 webhook 地址（回车跳过稍后手填）: ").strip()
                if args.webhook:
                    cfg["webhook"] = args.webhook
            except EOFError:
                pass
        write_json(cfg_path, cfg)
        print(f"[ok] 已生成 {cfg_path}" + ("" if cfg.get("webhook") else " —— 注意：webhook 还没填！"))

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
    our_norm = {os.path.normcase(os.path.join(SCRIPTS_DIR, n))
                for n in ("notify.py", "heartbeat_spawn.py")}

    def is_ours(group):
        """识别本工具的旧条目（用于幂等重装，避免重复挂载）。"""
        if not isinstance(group, dict):
            return False
        for h in (group.get("hooks") or []):
            for a in ((h or {}).get("args") or []):
                if os.path.normcase(a) in our_norm:
                    return True
        return False

    for event in HOOK_EVENTS:
        entry_script = "heartbeat_spawn.py" if event == "UserPromptSubmit" else "notify.py"
        kept = [g for g in (events.get(event) or []) if not is_ours(g)]
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

    # 4) 测试推送（可选）
    cfg_now = read_json(cfg_path, {})
    if args.test:
        if str(cfg_now.get("webhook", "")).startswith("https://qyapi"):
            sys.path.insert(0, SCRIPTS_DIR)
            try:
                from notify import send_wecom_once
                ok, detail = send_wecom_once(cfg_now["webhook"], "🩺 安装成功",
                                             "zcode-task-notify 已安装，任务通知将推送到这个群。")
                print(f"[{'ok' if ok else 'FAIL'}] 测试推送: {detail}（看一眼企业微信群）")
            except Exception as e:
                print(f"[FAIL] 测试推送异常: {e}")
        else:
            print("[skip] --test 已指定但 webhook 为空，无法测试")

    print("""
安装完成。接下来：
1. 若 webhook 还没填：编辑 scripts/config.json 的 "webhook" 字段
2. 重启 ZCode 桌面端（或直接新建一个会话）—— hooks 对新会话生效
3. 在新会话里随便跑个任务，企业微信应收到「✅ 任务完成」推送
4. 想关推送：改 scripts/state.json 的 enabled，或在 ZCode 微信 Bot 里说一声
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
