# ZCode Task Notify

给 [ZCode](https://bigmodel.cn/glm-coding) 桌面端加上**企业微信任务通知**：任何会话的任务完成、出错、需要确认时，你的手机企业微信自动收到一条带语义摘要的推送。跑长任务不用盯屏幕，人离开电脑也不错过任何节点。

```
✅ 任务完成（耗时 23 分钟）｜项目文档
   报告已生成：共 12 个章节，数据更新至 Q3。
   > 14:20 · sess_xxxxxxxx（会话 ID 截断展示）
```

## 功能

- **三类事件推送**：任务完成 ✅ / 任务出错 ❌ / 等待确认 ⏸️，完成与出错的标题带任务耗时（按本地数据库的回合真实状态区分，取消不推）
- **手机批准/拒绝**（v1.1 可选）：权限请求推到手机，点「批准/拒绝」按钮直接放行或拦截，人不在电脑前也能处理（企业微信智能机器人长连接，同样免费）
- **LLM 语义摘要**：默认接 GLM-4.5-Flash（免费模型）生成一句话摘要，能理解创作类内容；未配置或调用失败自动回退内置启发式提取
- **长任务心跳**：回合运行超过 30 分钟自动推「⏳ 仍在运行」，结束自动停止，防误报
- **手动开关**：给机器人发「静默/恢复/状态」即可全量静默/恢复（未启用手机批准时改 `state.json`）
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
- Python 3.10+（仅标准库，无第三方依赖；**安装时记得勾选 "Add Python to PATH"**）
- 手机安装**企业微信 App**（接收通知的终点）
- 一个企业微信群机器人 webhook（免费，获取步骤见下）

## 第一步：获取企业微信 Webhook（免费，约 5 分钟）

> 企业微信 = 腾讯面向企业的办公 App，**个人使用完全免费**：无需营业执照、无需企业认证、无需付费。通知会发到企业微信 App，不是个人微信——这是"不花钱、不限量、不封号"的代价。

**1. 注册并下载企业微信**

- 电脑访问 [work.weixin.qq.com](https://work.weixin.qq.com) 点「立即注册」，或手机应用商店直接搜索「企业微信」下载；
- 注册只需手机号 + 填一个企业名称（个人使用随便填，如「我的通知」），**不需要认证**；
- 注册完成后，在**手机**上登录企业微信 App——通知最终推送到这里。

**2. 建一个群**

- 企业微信 App → 消息页 → 右上角「+」→ 发起群聊；
- 如果提示「至少选择一名成员」（新账号没有同事时常见），先拉一位家人/朋友进群，**建好后把对方移出即可**——群不会因此解散，机器人继续可用。

**3. 添加群机器人，复制 Webhook 地址**

- 入口因企业微信版本而异：旧版在 群 → 右上角「···」→「群机器人」→「添加机器人」→ 新建；新版手机端在 群 → 右上角「···」→「聊天信息」→ **「消息推送」**→「添加自定义消息推送」；
- 命名随意（如「ZCode 通知」）→ 创建/保存后会显示一个 **Webhook 地址**，形如：
  `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx`
- 复制它，**这就是安装脚本要填的东西**。

手机端实测路径（2026-09 版企业微信）：

| 群「聊天信息」里的「消息推送」入口 | 「添加自定义消息推送」，点 Webhook 地址复制 |
|---|---|
| <img src="docs/images/wecom-step1-group-entry.png" width="270"> | <img src="docs/images/wecom-step2-add-webhook.png" width="270"> |

**4. 先测一下能不能收到（可选但推荐）**

把下面命令里的地址换成你的 webhook，用 Python 执行（UTF-8 编码可靠，中文不会乱码）：

```bash
python -c "import json,urllib.request; req=urllib.request.Request('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key', data=json.dumps({'msgtype':'text','text':{'content':'测试消息 from zcode-task-notify'}}).encode('utf-8'), headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(req).read().decode())"
```

手机企业微信收到「测试消息」即通道打通。输出 `{"errcode":0,"errmsg":"ok"}` 同样代表成功。

> ⚠️ Webhook 地址等同「往这个群发消息的钥匙」，**不要发到公开场合**或提交到 git（本仓库的 .gitignore 已排除含 key 的 config.json）。

## 第二步：安装

clone 本仓库（若 GitHub 访问慢，可在仓库页点 **Code → Download ZIP**）后，运行安装脚本（webhook 用第一步拿到的地址）：

```bash
python install.py --webhook "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key"
```

脚本会：生成 `scripts/config.json` 与开关文件、**自动备份并合并** hooks 配置到 `%USERPROFILE%\.zcode\cli\config.json`（只改 `hooks` 键，其他配置和已有的其他 hook 原样保留，并有带时间戳的备份；重复运行不会重复挂载）。

**（可选）接入 LLM 摘要**：在 [bigmodel.cn 控制台](https://bigmodel.cn/console/usercenter/apikeys) 创建一个 API Key（免费注册），然后编辑 `scripts/config.json`：

```json
{
  "use_llm_summary": true,
  "llm_api_key": "你的bigmodel key（形如 xxx.yyy）",
  "llm_base_url": "https://open.bigmodel.cn/api/paas/v4",
  "llm_model": "glm-4.5-flash"
}
```

## 第三步：验证

**在 ZCode 里新建一个会话**，随便发一句话。该会话回复结束时，企业微信应收到「✅ 任务完成」推送，长这样（图为 `doctor.py` 的自检测试消息）：

<img src="docs/images/wecom-test-success.jpg" width="380">

> 为什么必须新建会话：hooks 配置在会话进程启动时加载，改动只对新会话生效（见下方「已知坑」第 1 条）。

## 进阶：手机批准/拒绝（可选，v1.1）

权限请求发生时，手机企业微信收到一张带「✅ 批准 / ❌ 拒绝」按钮的卡片，点一下即放行或拦截；120 秒不操作自动回退到电脑上的正常确认弹窗。基于企业微信「智能机器人」**长连接**（免费、无需公网服务器），与上面的群机器人通知互相独立、互不影响。

1. 手机企业微信 → 工作台 → 智能机器人 → 创建机器人 → 选 **API 模式** → **使用长连接** → 点「随机获取」生成 Secret → **保存**；
2. `python install.py --aibot --bot-id "..." --bot-secret "..."`（两者在 机器人详情 → API设置 页复制）；
3. 给这个机器人**发一条单聊消息**（如"你好"）——连接器自动捕获你的推送地址并就绪。

运行机制：权限请求 → hook 经本地连接器（`127.0.0.1:17899`）发卡片 → 你点按钮 → 决定回写给 hook → ZCode 继续/停止，卡片同时原位变成结果卡并锁定。连接器由心跳 hook 看门狗守护（发消息时自动拉起），断线由 SDK 自动重连。安全边界：卡片只发你自己的单聊；决定文件仅存本地且 15 分钟过期；webhook 通知与按钮决策互为降级（连接器不在线时自动退回普通 ⏸️ 通知）。

> 卡片决策同样只对**新会话**生效（见已知坑 1）。

## 配置项（`scripts/config.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `webhook` | 必填 | 企业微信群机器人地址 |
| `use_llm_summary` | `false` | 开启 LLM 语义摘要（失败自动回退启发式） |
| `llm_api_format` | `openai` | `openai`（bigmodel v4）或 `anthropic` |
| `llm_model` | `glm-4.5-flash` | 摘要模型（免费） |
| `heartbeat_interval_min` | `30` | 心跳间隔（分钟） |
| `heartbeat_max_hours` | `4` | 心跳最长跟踪时长（小时） |

开关：`scripts/state.json` 的 `enabled` 字段（`true`/`false`）；启用手机批准（见进阶章节）后，直接给机器人发「静默」「恢复」「状态」即可遥控开关。

## 自检与卸载

收不到通知时，一条命令定位问题（16 项检查：Python 版本 / 桌面端运行 / 开关 / webhook 配置与通道实测 / hooks 注册 / 顶层键毒化 / 脚本完整性 / LLM key / aibot 手机批准组件）：

```bash
python doctor.py                 # 含企业微信通道实测（会发一条测试消息）
python doctor.py --no-send       # 静默检查，不发测试消息
```

卸载（移除本工具注册的 hooks，保留其他配置和已有 hook，并自动备份）：

```bash
python install.py --uninstall
```

之后删除仓库目录即可（无残留、无系统级安装）。若 `--uninstall` 不可用（比如手动改过配置），备选方案：

1. 用 `%USERPROFILE%\.zcode\cli\config.json` 旁的备份文件（`config.json.bak-<时间戳>`）覆盖回去，或手动把 `hooks.events` 里 `args` 指向本工具 `scripts` 的条目删掉；
2. （可选）进入企业微信群 → 群机器人/消息推送 → 移除机器人。

安装时也可加 `--test` 让脚本装完立即发一条测试消息验证通道。

## 已知坑与设计决策（实测踩出来的，重要）

1. **hooks 配置在会话的 agent 进程启动时读取一次，进程存活期不重读**。改配置后新会话立即生效，已启动的会话要等 ZCode 重启。测试新配置务必开全新会话，在老会话里测会得出"不生效"的错误结论。
2. **`~/.zcode/cli/config.json` 对顶层键严格校验**：放入 hooks 以外的未知顶层键（如 `provider`）会导致 hooks 配置整体静默失效——没有任何报错，就是不执行（2026-09-14 实测复发：桌面端写入模型设置时把 `provider` 写回该文件顶层，重启后 hook 全部消失）。现已内置两级防护：`doctor.py` 第 13 项会检查毒键；**自愈机制**——每次用户提交消息时心跳 hook 检测到已知毒键会自动备份（`config.json.bak-*-selfheal`）、移除并推送「🔧 hooks 配置自愈」告知。该文件只应有 `plugins`、`hooks` 等官方键。
3. **glm-4.5-flash 是思考模型**：v4 接口请求必须带 `"thinking": {"type": "disabled"}`，否则思考链吃光 `max_tokens`、正文为空且容易超时。
4. **zcode-plan 端点（`zcode.z.ai/api/v1/zcode-plan/*`）有 captcha 防护**，脚本直调会返回 `{"code":3007,"msg":"captcha verify failed"}`——该通道仅限桌面端内部使用，外部脚本应走 open.bigmodel.cn 标准 v4 接口。
5. **Stop hook 的 payload 没有成功/失败标志**，需按 `turnId` 查本地 `turn_usage` 表获取真实状态（`completed/error/cancelled`）。
6. 企业微信**群机器人 webhook 是单向通道**（通知只能看不能点）；双向交互（手机批准/拒绝）走的是另一条通道——智能机器人长连接（见「进阶：手机批准/拒绝」），两者互不影响。

## 故障排查

| 现象 | 排查 |
|---|---|
| 完全收不到推送 | 看 `notify_log.jsonl`：无记录=hook 未触发（确认会话是新建的）；有记录=看 `skipped`/`error` 原因 |
| 推送时有时无 | 确认触发会话是在 hooks 配置写入**之后**启动的（见坑 1） |
| `push_failures.log` 有内容 | 企业微信侧问题：webhook key 是否被重置、网络是否可达 |
| LLM 摘要没生效 | 看 `notify_log.jsonl` 的 `llm_error`；确认 `llm_api_key` 有效、未超免费限速 |

## License

[MIT](LICENSE)
