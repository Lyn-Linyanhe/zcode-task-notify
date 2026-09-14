# AGENTS.md

本文件写给 AI agent。先看表选段落：

| 你的任务 | 去哪一段 |
|---|---|
| 用户让你**把这套工具装到这台电脑上** | [A. 安装执行手册](#a-安装执行手册) |
| 用户让你**修改这个仓库的代码** | [B. 开发约定](#b-开发约定) |

人类用户请看 [README.md](README.md)；本文是给 agent 的执行步骤，语气是命令式的。

---

## A. 安装执行手册

### A0. 先问用户要什么（你替不了他）

- **企业微信群机器人 webhook**（必需）——只能用户自己在手机企业微信里建：
  群 → 右上角「···」→「消息推送」→「添加自定义消息推送」→ 创建后复制那串
  `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…`
- （可选）手机批准/拒绝：需要另一个「智能机器人」的 Bot ID + Secret，README「进阶」有图。
  **默认不做**，除非用户明确要。

拿不到 webhook 就停下来问，**不要编造地址、也不要假装装好了**。

### A1. 达到这些才算装完

1. 仓库位于**稳定目录**——推荐 `%USERPROFILE%\.zcode\task-notify`。
   **不要留在 Downloads、桌面或临时目录**：hooks 里记录的是脚本的绝对路径，
   目录被挪走或被清理会让注册失效，甚至留下重复注册。
2. `scripts/config.json` 的 `webhook` 是用户给的那串地址。
3. `%USERPROFILE%\.zcode\cli\config.json` 中 `hooks.enabled = true`，
   且 `hooks.events` 里 **Stop / PermissionRequest / UserPromptSubmit 各一条**：
   前两个指向本目录的 `scripts\notify.py`，第三个指向 `scripts\heartbeat_spawn.py`。
4. `python doctor.py` 输出 **0 FAIL**——全装好（含手机批准）是 16 PASS；
   **未启用手机批准时是 14 PASS，属正常**，不要因为不是 16 就判定失败。
   它会真的往群里发一条消息，跑之前跟用户说一声；只想静默检查用 `python doctor.py --no-send`。
5. 用户手机上收到「🩺 安装成功」。

### A2. 步骤

```bash
# 0) 环境自检
python --version           # 需 >= 3.10（Windows）。没有 Python 就停下，让用户装：
                           #   https://www.python.org/downloads/  安装时务必勾选 "Add Python to PATH"
                           # 再确认 ZCode 桌面端装过：%USERPROFILE%\.zcode 目录存在

# 1) 取代码到稳定目录（没装 git 就让用户从仓库页 Code → Download ZIP 解压到同一路径）
git clone https://github.com/Lyn-Linyanhe/zcode-task-notify "%USERPROFILE%\.zcode\task-notify"
cd /d "%USERPROFILE%\.zcode\task-notify"

# 2) 安装（会先备份、只改 hooks 键；重复运行安全）
python install.py --webhook "用户给的地址"
#    用户还要 AI 摘要   → 追加 --llm-key "xxx.yyy"
#    用户还要手机批准   → 追加 --aibot --bot-id "aib…" --bot-secret "…"

# 3) 自检（含向群里真实推一条）
python doctor.py
```

4) **收尾必须说清**：hooks 只对**新启动的会话**生效 —— 让用户在 ZCode 里**新建一个会话**
   随便问一句，结束时应收到「✅ 任务完成（耗时 xx）」。在老会话里测会得出"不生效"的错误结论。

### A3. 硬性禁令（都是实测踩出来的，违反会静默坏事）

- **绝不往 `~/.zcode/cli/config.json` 顶层加任何键**，尤其不要加 `provider`：
  桌面端对其严格校验，多一个未知顶层键会让**全部 hooks 静默失效**（没有任何报错）。
  这个文件只应有 `hooks`、`plugins` 等官方键，本工具也只写 `hooks`。
- 不要删改 `hooks.events` 里**不属于本工具**的条目（插件 hook 等）。
- 不要把 webhook、Bot Secret、LLM key 写进任何会被提交或外发的文件；
  `.gitignore` 已排除 `config.json` / `aibot_config.json`，别把它们挪到别处。
- 不要 `git push --force` / rebase / 丢弃用户已有改动。
- 装完不要"顺手"改用户的其他配置（`state.json` 的开关、`config.json` 其他字段等）。

### A4. 常见故障

| 现象 | 处理 |
|---|---|
| `python` 不是内部或外部命令 / 版本 < 3.10 | 让用户去 python.org 装，**务必勾选 "Add Python to PATH"**；装完关掉所有终端窗口再重试。仓库里的 `install.bat` 双击可自动做这套引导 |
| 缺 webhook | 只能用户自己去企业微信建群机器人；不要编造地址，也不要跳过 |
| 装完没收到通知 | ① 确认是**新开会话**；② `python doctor.py` 逐项看；③ 看 `scripts\notify_log.jsonl`：无记录 = hook 没触发，有 `skipped`/`error` 字段 = 按字段定位 |
| 目录挪过 / 重新下载过 | 重跑 `python install.py`，它会自动清掉指向旧位置的重复注册（旧注册还在跑会造成"通知时有时无"） |
| 想临时静音 | 启用手机批准后给机器人发「静默」；否则改 `scripts/state.json` 的 `enabled`（或让机器人发「静默 30」定时） |
| 通知里没有耗时 / 出错显示成 ✅ | 属已知坑（见 README 已知坑 5）：`turn_usage` 行落库有延迟，`stop_status_wait_sec`（默认 60 秒）cover 它 |
| 卸载 | `python install.py --uninstall`（只移除本工具的 hook 条目，保留其他配置并自动备份） |

### A5. 完成后向用户汇报

一段话即可：装在哪、验证结果（doctor 的 PASS 数）、怎么验证真实推送、怎么静音、怎么卸载，
以及可选功能（AI 摘要 / 手机批准）各自怎么开。

---

## B. 开发约定

- **主线零第三方依赖**（仅标准库）；只有「手机批准/拒绝」用独立 venv 装 `wecom-aibot-python-sdk`
  （版本已钉住，`reply_stream` 语义是正确性前提，别放开约束）。
- 改完必须跑：`python -m unittest discover -s tests`（60+ 例）。
  其中 aibot 那组需要真实安装，用 `ZCODE_NOTIFY_SCRIPTS=<安装目录>` + 该目录的 venv 解释器跑。
- 改了 hook 相关逻辑，注意 `hooks.events` 是 `[{matcher?, hooks:[定义]}]` 的**嵌套**结构（strict 校验），
  扁平写法会被静默拒绝。
- 提交信息用中文、说清"为什么"（现象 → 根因 → 修法）；CI 为 windows-latest × Python 3.10/3.12。
- 新增踩坑结论请同步进 README 的「已知坑」，那是这个项目最有价值的部分。
- 仓库里**不允许**出现真实凭证；`config.json` / `aibot_config.json` 已在 `.gitignore`。
