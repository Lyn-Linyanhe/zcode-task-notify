# ZCode Task Notify

给 [ZCode](https://bigmodel.cn/glm-coding) 桌面端加上**企业微信任务通知**：任何会话的任务完成、出错、需要确认时，你的手机企业微信自动收到一条带语义摘要的推送。跑长任务不用盯屏幕，人离开电脑也不错过任何节点。

```
✅ 任务完成｜项目文档
   报告已生成：共 12 个章节，数据更新至 Q3。
   > 14:20 · sess_xxxxxxxx（会话 ID 截断展示）
```

## 功能

- **三类事件推送**：任务完成 ✅ / 任务出错 ❌ / 等待确认 ⏸️（按本地数据库的回合真实状态区分，取消不推）
- **LLM 语义摘要**：默认接 GLM-4.5-Flash（免费模型）生成一句话摘要，能理解创作类内容；未配置或调用失败自动回退内置启发式提取
- **长任务心跳**：回合运行超过 30 分钟自动推「⏳ 仍在运行」，结束自动停止，防误报
- **手动开关**：微信里说一声（或改状态文件）即可全量静默/恢复
- **智能过滤**：微信 Bot 会话不重复推送；子代理会话过滤；推送失败自动重试并落失败日志

## 工作原理

```
ZCode hooks（会话进程启动时加载，Stop/PermissionRequest/UserPromptSubmit）
├─ notify.py        hook 入口：秒回，落盘 payload，分离进程启动 worker
│     └─ push_worker.py
│          ├─ 过滤：开关 / 微信Bot会话 / 子代理会话
│          ├─ 状态：查 turn_usage 表，区分 ✅完成 / ❌出错 / ⏹️已取消(静默)
│          ├─ 摘要：LLM（GLM-4.5-Flash，失败自动回退启发式提取）
│          └─ 推送：企业微信群机器人 webhook（失败重试 1 次）
└─ heartbeat_spawn.py → heartbeat.py（分离进程）
       └─ 心跳循环：回合未结束且超间隔 → 推「⏳ 仍在运行」；结束即退出
```

## 环境要求

- Windows 10/11（使用了 Win32 进程枚举与分离进程 API）
- ZCode 桌面端 **3.11.2+**（hooks schema 在此版本验证）
- Python 3.10+（仅标准库，无第三方依赖）
- 一个企业微信群机器人 webhook（免费，见下）

## 安装

1. 建一个企业微信群机器人：企业微信 App 建群（可以只有你自己）→ 群设置 → 群机器人 → 添加 → 复制 Webhook 地址；
2. clone 本仓库，运行安装脚本：

```bash
python install.py --webhook "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key"
```

脚本会：生成 `scripts/config.json` 与开关文件、**自动备份并合并** hooks 配置到 `%USERPROFILE%\.zcode\cli\config.json`（只动 `hooks` 键，不碰其他配置）。

3. （可选）接入 LLM 摘要：在 [bigmodel.cn 控制台](https://bigmodel.cn/console/usercenter/apikeys) 创建一个 API Key，然后编辑 `scripts/config.json`：

```json
{
  "use_llm_summary": true,
  "llm_api_key": "你的bigmodel key（形如 xxx.yyy）",
  "llm_base_url": "https://open.bigmodel.cn/api/paas/v4",
  "llm_model": "glm-4.5-flash"
}
```

4. **在 ZCode 里新建一个会话**测试（hooks 配置在会话进程启动时加载，改动只对新会话生效）。

## 配置项（`scripts/config.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `webhook` | 必填 | 企业微信群机器人地址 |
| `use_llm_summary` | `false` | 开启 LLM 语义摘要（失败自动回退启发式） |
| `llm_api_format` | `openai` | `openai`（bigmodel v4）或 `anthropic` |
| `llm_model` | `glm-4.5-flash` | 摘要模型（免费） |
| `heartbeat_interval_min` | `30` | 心跳间隔（分钟） |
| `heartbeat_max_hours` | `4` | 心跳最长跟踪时长（小时） |

开关：`scripts/state.json` 的 `enabled` 字段（`true`/`false`）。

## 已知坑与设计决策（实测踩出来的，重要）

1. **hooks 配置在会话的 agent 进程启动时读取一次，进程存活期不重读**。改配置后新会话立即生效，已启动的会话要等 ZCode 重启。测试新配置务必开全新会话，在老会话里测会得出"不生效"的错误结论。
2. **`~/.zcode/cli/config.json` 对顶层键严格校验**：放入 hooks 以外的未知顶层键（如手工加的 `provider`）会导致 hooks 配置整体静默失效——没有任何报错，就是不执行。该文件只应有 `plugins`、`hooks` 等官方键。
3. **glm-4.5-flash 是思考模型**：v4 接口请求必须带 `"thinking": {"type": "disabled"}`，否则思考链吃光 `max_tokens`、正文为空且容易超时。
4. **zcode-plan 端点（`zcode.z.ai/api/v1/zcode-plan/*`）有 captcha 防护**，脚本直调会返回 `{"code":3007,"msg":"captcha verify failed"}`——该通道仅限桌面端内部使用，外部脚本应走 open.bigmodel.cn 标准 v4 接口。
5. **Stop hook 的 payload 没有成功/失败标志**，需按 `turnId` 查本地 `turn_usage` 表获取真实状态（`completed/error/cancelled`）。
6. 企业微信群机器人 webhook 是单向通道：收到「等待确认」后，仍需切到微信 Bot 会话或电脑前处理，不能在通知卡片上直接操作。

## 故障排查

| 现象 | 排查 |
|---|---|
| 完全收不到推送 | 看 `notify_log.jsonl`：无记录=hook 未触发（确认会话是新建的）；有记录=看 `skipped`/`error` 原因 |
| 推送时有时无 | 确认触发会话是在 hooks 配置写入**之后**启动的（见坑 1） |
| `push_failures.log` 有内容 | 企业微信侧问题：webhook key 是否被重置、网络是否可达 |
| LLM 摘要没生效 | 看 `notify_log.jsonl` 的 `llm_error`；确认 `llm_api_key` 有效、未超免费限速 |

## License

[MIT](LICENSE)
