# claude-code-im-channel

把 **Telegram / Discord / 飞书（Lark）** 桥接到 Claude Code CLI 会话，作为
独立的 systemd 守护进程跑——**不依赖 tmux、不依赖 MCP `--channels` 插件、
不受 firstParty 闸限制**。任何能跑 `claude -p` 的环境都能用，包括
sub2api / OneAPI / Bedrock / Vertex 中转。

> English README → [README.md](./README.md)

---

## 为什么要造这个轮子

Claude Code 官方的 Telegram / Discord 通道插件是 `claude` 二进制通过
`--channels` 机制启动的 MCP server。这套设计有两个硬伤：

1. Bot 连接寄生在 `claude` 进程里——`claude` 崩了、被杀了、退出了，bot 也
   一起消失，入向消息直接丢。
2. `--channels` 被 Anthropic 内部 `firstParty()` 闸锁定。任何走中转端点
   (sub2api / OneAPI / new-api / Bedrock / Vertex) 的部署都用不了。

常见的绕法是把 `claude --channels plugin:telegram` 塞到 tmux 里跑，靠终端
复用器保活。但这没解决 #2，而且一旦 claude 卡在工具循环里，bot 的事件
循环也会被一起拖垮。

本项目走的是相反的路线：

- Daemon 拥有 bot 连接，把 `(platform, chat_id) → claude_session_id` 持久化
  在 SQLite 里；每收到一条消息就 spawn 一个独立的
  `claude -p --output-format stream-json --resume <session_id>` 子进程。
- 单条消息的工具循环再长也只是子进程的事，bot 一直在轮询。
- systemd `Restart=always` 兜底崩溃；`config.yaml` mtime 一变就热加载。

因为根本不碰 `--channels`，firstParty 闸跟我们没关系。能跑 `claude -p` 就
能跑这个 daemon。

---

## 功能特性

- ✅ **三平台并启** —— Telegram (aiogram) / Discord (discord.py) / 飞书 (lark-oapi)
- ✅ **按 chat 维度持久化 session** —— 每个 chat（私聊或群）有自己的 claude
  session_id，daemon 重启不丢
- ✅ **流式进度卡片** —— 单条 placeholder 消息原地编辑，实时显示
  `tool_use` / `text` / `tool_result` 事件
- ✅ **完整 CLI `/` 命令支持**
  - daemon 自己拦截 `/new` `/cancel` `/status` `/sessions` `/log` `/menu`
    `/help`（控制 daemon 状态）
  - 其他所有 `/cmd`（`/review` `/init` `/security-review` `/simplify`
    `/ultrareview` 以及任何用户自定义 skill）作为 prompt 透传给 `claude -p`，
    由 claude 自己的 skill 层路由
- ✅ **双向附件**
  - 入：用户在 IM 里发的图片/文件被下载到本地，路径作为 `Read` 提示透传
    给 claude
  - 出：claude 在回复里写 `[ATTACH:/abs/path]` 标记，桥接会把这段标记剥
    出来并把文件作为独立消息发回
- ✅ **自动重启、自动错误兜底** —— `Restart=always` + `ClaudeRunError` 统一
  封包成中文友好提示
- ✅ **空闲 / 总超时双闸** —— 默认 15 分钟空闲 / 2 小时总时长，超过就 kill
  子进程并返回已累积的文本
- ✅ **逐平台白名单** —— 每个 adapter 独立 `allowed_user_ids`；群聊可要求
  `@bot` 才回应

---

## 快速开始

### 1. 安装

```bash
git clone https://github.com/ztllll/claude-code-im-channel.git
cd claude-code-im-channel
./deploy/install.sh
```

`install.sh` 会自动建 `.venv` 并安装基础包，最后打印下一步。要把三个
adapter 的依赖一次装齐：

```bash
.venv/bin/pip install -e '.[all]'
```

或者按需选装：

```bash
.venv/bin/pip install -e '.[discord]'     # 加 discord.py
.venv/bin/pip install -e '.[feishu]'      # 加 lark-oapi
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

打开要用的平台（`enabled: true`），填好 token / app_secret，把你的 IM
账号 id 加到 `allowed_user_ids`。token 也可以走环境变量：

```bash
export TELEGRAM_BOT_TOKEN="..."
export DISCORD_BOT_TOKEN="..."
export FEISHU_APP_ID="cli_..."
export FEISHU_APP_SECRET="..."
```

环境变量优先级高于 yaml 文件 —— 配合 systemd `EnvironmentFile=` 用比较干净。

#### 平台配置要点

| 平台 | 凭证来源 | 备注 |
|------|---------|------|
| Telegram | `@BotFather` 拿 bot token | 群里要让 bot 看见消息：要么 disable privacy mode，要么 @ 它 |
| Discord | Developer Portal 拿 bot token | **必须开启 `message_content` 特权 intent**，否则机器人收不到消息内容 |
| 飞书 | https://open.feishu.cn/app → 应用详情 → 凭证与基础信息 | 自建应用，长连接模式无需开 inbound 端口；权限要勾 `im:message` + `im:resource` |

### 3. 继承已有 tmux session（可选）

如果你之前是用 `claude --channels plugin:telegram` 在 tmux 里跑的，并且
想保留之前的对话上下文，可以让 daemon 接管现有 session：

```bash
.venv/bin/python -m im_claude_channel import-tmux-sessions --dry-run   # 预览
.venv/bin/python -m im_claude_channel import-tmux-sessions             # 应用
```

它会扫描 `~/.claude/projects/*/` 里所有 `.jsonl`，找出带
`<channel source="..." chat_id="...">` 标签的 session，按
`(platform, chat_id)` 维度选最近活跃的那一条种到 daemon 的 SQLite 里。

> 飞书没有对应的 import 路径——飞书从来没法通过 `--channels` 启动，所以
> 不存在 tmux session 可继承。

### 4. 启动

```bash
mkdir -p ~/.config/systemd/user
cp systemd/im-claude-channel.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now im-claude-channel.service
journalctl --user -fu im-claude-channel.service
```

---

## 斜杠命令分类

命令分三类：

| 类型 | 例子 | 行为 |
|------|------|------|
| **Daemon 拦截** | `/new` `/cancel` `/status` `/sessions` `/log` `/help` `/menu` | 操作 daemon 自己的状态，绝对能用，秒回 |
| **Skill 透传** | `/review` `/init` `/security-review` `/simplify` `/ultrareview` `/doctor` 以及任何自定义 skill | `/cmd` 整段作为 prompt 喂给 `claude -p`，由 claude 自己的 skill 层解析（你需要在 Claude Code 里实际安装/启用对应 skill） |
| **TUI 限定**（`/help` 里标 `[TUI]`） | `/clear` `/compact` `/model` `/permissions` `/config` … | 这些只对 Claude Code 交互式 TUI 有意义，在 `-p` 模式下基本只能得到文字解释而非真操作。`/clear` 的对等替代是 daemon 的 `/new` |

`/menu` 弹出的是策划好的分组卡片（按"daemon 控制 / 审查 / 仓库 PR /
诊断信息 / 记忆计划 / 配置助手"分组），是给手机端用的速查表。

完整 60+ 命令的展示方式：
- Telegram：直接打 `/` 触发自带的命令自动补全（已通过 `setMyCommands` 注册）
- Discord：发 `/help`（v0.2 没注册 app slash command）
- 飞书：发 `/menu` 看分组卡片，或 `/help` 看完整清单

---

## 架构

```
                   ┌────────────────────────────────────┐
                   │  claude-code-im-channel  (daemon)  │
                   │                                    │
   ┌─Telegram──────┼─►┐                                 │
   │   (aiogram)   │  │                                 │
   │               │  ├─► 鉴权 / 去重 / 命令解析        │
   ┌─Discord───────┼─►┤   daemon 命令 ─► 拦截执行       │
   │  (discord.py) │  │                              │  │
   │               │  ├─► claude_runner.run_stream   │  │
   ┌─飞书──────────┼─►┘     │                        │  │
   │  (lark-oapi)  │        ▼                        │  │
   │               │   ┌────────────────┐            │  │
   │               │   │ claude -p      │            │  │
   │               │   │   --resume sid │            │  │
   │               │   │   stream-json  │            │  │
   │               │   └────────────────┘            │  │
   │               │        │                        │  │
   │               │        ▼ (一行一个 NDJSON 事件)  │  │
   │               │   placeholder 卡片随事件原地编辑─┘  │
   └───────────────┘                                    │
                   │  SQLite: (platform, chat_id)       │
                   │   ↔ session_id, message_count      │
                   └────────────────────────────────────┘
```

关键文件：

| 文件 | 作用 |
|------|------|
| `src/im_claude_channel/server.py` | Daemon 主循环、按 chat 加锁、worker 派发、`[ATTACH]` 处理 |
| `src/im_claude_channel/claude_runner.py` | `claude -p` 子进程 + NDJSON 流式解析 + heartbeat + 取消 |
| `src/im_claude_channel/session_store.py` | SQLite session 表，`(platform, chat_id)` 为主键 |
| `src/im_claude_channel/commands.py` | Daemon 命令分发 + 完整 MENU 列表 + `/menu` 卡片文案 |
| `src/im_claude_channel/access.py` | 逐平台白名单 + 群提及策略 |
| `src/im_claude_channel/importer.py` | 从 tmux/MCP `<channel>` 标签播种 session_store |
| `src/im_claude_channel/adapters/base.py` | `Adapter` Protocol + `IncomingMessage` dataclass |
| `src/im_claude_channel/adapters/telegram.py` | aiogram 长轮询适配器 |
| `src/im_claude_channel/adapters/discord.py` | discord.py 网关适配器 |
| `src/im_claude_channel/adapters/feishu.py` | lark-oapi 长连接 + REST 客户端 + interactive card |

Adapter Protocol 故意做得很薄——只要求实现 `send_message` `edit_message`
`send_file` `set_menu`。平台特有的脏活（飞书 PATCH 只接受 card 类型、
Discord 的 `message_content` intent、Telegram 的多 scope `setMyCommands`）
都塞在各自 adapter 里，daemon 主路径完全平台无关。

---

## 添加新平台（比如微信 / 钉钉 / QQ）

1. 在 `src/im_claude_channel/adapters/<platform>.py` 实现 `Adapter` Protocol。
2. 在 `config.py` 加 `<Platform>Config` dataclass，加进 `Config.load`。
3. 在 `Daemon._make_adapters` 里加分支构造它。
4. 在 `config.example.yaml` 加对应的 section。

就这样。session 存储、命令分发、鉴权、进度卡片、附件标记、错误兜底都
是平台无关的，自动继承。

---

## 路线图（v0.3+）

- Discord application (slash) command 注册
- 飞书可选的菜单卡片首次置顶
- 微信 / 钉钉 / QQ adapter（欢迎社区贡献）
- Permission relay：把 claude 的权限请求转发到 IM 上人工放行，而非
  `--dangerously-skip-permissions` 一刀切
- 飞书流式编辑（用 card 多 section 模板替代 markdown 单元素）

---

## 项目沿革

本仓库是 [claude-code-feishu-channel](https://github.com/ztllll/claude-code-feishu-channel)
的合并升级版——飞书相关代码已并入 `adapters/feishu.py`，老仓库进入只读
维护状态。两者部署位置不同步骤一致，老仓库部署机器可用以下方式平滑切换：

```bash
# 1) 停老服务
systemctl --user stop feishu-claude-channel.service

# 2) 拉新仓
git clone https://github.com/ztllll/claude-code-im-channel.git ~/claude-code-im-channel

# 3) 把老 config.yaml 的飞书字段搬到新 config.yaml 的 feishu: 段下
cd ~/claude-code-im-channel
cp config.example.yaml config.yaml
$EDITOR config.yaml

# 4) 用新 systemd unit 起来
./deploy/install.sh
.venv/bin/pip install -e '.[feishu]'
mkdir -p ~/.config/systemd/user
cp systemd/im-claude-channel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now im-claude-channel.service
```

---

## 许可证

Apache-2.0。
