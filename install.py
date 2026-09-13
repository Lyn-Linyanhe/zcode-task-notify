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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default="", help="企业微信群机器人 webhook 地址")
    ap.add_argument("--target", default=os.path.expanduser("~"), help=".zcode 所在根目录（默认用户主目录）")
    args = ap.parse_args()

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
    for event in HOOK_EVENTS:
        entry_script = "heartbeat_spawn.py" if event == "UserPromptSubmit" else "notify.py"
        events[event] = [{
            "type": "process",
            "command": sys.executable or "python",
            "args": [os.path.join(SCRIPTS_DIR, entry_script)],
            "timeoutMs": 5000,
        }]
    hooks["events"] = events
    existing["hooks"] = hooks
    write_json(zcode_cfg, existing)
    print(f"[ok] hooks 已写入 {zcode_cfg}（事件: {', '.join(HOOK_EVENTS)}）")

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
