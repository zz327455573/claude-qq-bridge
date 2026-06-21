# QQ AI Bridge（QQ 机器人桥接 AI Agent）

通过 QQ 官方机器人 WebSocket 网关，将 QQ 消息直连本地 AI Agent（Codex / Claude Code / AGY / Gemini），实现通过 QQ 与 AI 对话。

一个 QQ 号可以挂载多个机器人，各通道独立运行，互不干扰。

---

## 架构设计

```
QQ 用户发送指令
     |
腾讯官方 WebSocket 网关 (api.sgroup.qq.com)
     |
[codex_bridge.py / claude_bridge.py / agy_bridge.py] (桥接器)
     |  非交互式执行 (subprocess)
AI Agent CLI (Codex / Claude Code / AGY)
     |
结果返回 -> QQ 消息接口
```

---

## 功能特性

* **多 Agent 引擎支持**：
  * **Codex (OpenAI)**：运行 codex_bridge.py，利用 Codex 原生的 --json + exec resume 实现有状态会话续接，无需手动拼接历史。
  * **Claude Code (Anthropic)**：运行 claude_bridge.py，支持基于 --allowedTools 白名单的无阻塞推理与执行。
  * **AGY (Google Antigravity / Gemini)**：运行 agy_bridge.py，支持基于 --dangerously-skip-permissions 和 --conversation 的完整会话管理。
* **零外部依赖**：仅使用 aiohttp + httpx，无需安装 NapCatQQ、go-cqhttp 等第三方非官方客户端，安全合规，防封控。
* **多轮会话记忆**：各 Agent 引擎均有独立的会话管理机制，按用户的 user_openid 隔离维护连续对话。
* **安全隔离防御**：严格限制仅允许配置的 MASTER_OPENID（主理人）进行指令交互，防止其他人误用或恶意调用系统命令。
* **紧急刹车机制**：
  * 支持在 QQ 中发送 /stop、/停止、/kill 或 杠stop 强杀指令。
  * 桥接器会立即在进程级别终止后台运行的 Agent 子进程，并取消协程任务。
  * 内置忙碌防并发冲突保护。
* **便捷会话控制**：支持 /clear、/new（清空上下文，开启新会话）。

---

## 前置条件

1. **QQ 官方机器人**：在 [QQ 开放平台](https://q.qq.com) 注册机器人，获取 AppID 和 AppSecret。每个 Agent 需要独立的机器人。
2. **AI Agent CLI**：
   * 安装 OpenAI 的 codex 命令行工具。
   * 安装 Anthropic 的 claude 命令行工具。
   * 安装 Google Antigravity 的 agy 命令行工具。
3. **环境依赖**：Python 3.10+，以及 aiohttp、httpx 库。

---

## 安装与部署

### 1. 克隆与安装依赖

```bash
git clone https://github.com/zz327455573/qq-ai-bridge.git
cd qq-ai-bridge
pip install aiohttp httpx
```

### 2. 配置说明

分别编辑各桥接器顶部的配置区：

```python
# QQ 开放平台机器人凭证（每个 Agent 需要独立的机器人）
APP_ID = "你的AppID"
APP_SECRET = "你的AppSecret"

# 主理人标识（用户的 openid，首次运行后发送消息可从日志获取）
MASTER_OPENID = "你的QQ_openid"
```

### 3. 启动运行

建议使用 screen 在后台挂载运行：

```bash
mkdir -p ~/.screen && chmod 700 ~/.screen
export SCREENDIR=$HOME/.screen

# 运行 Codex 桥接服务
screen -dmS codex-bridge python3 -u codex_bridge.py

# 运行 AGY 桥接服务
screen -dmS agy-bridge python3 -u agy_bridge.py

# 运行 Claude Code 桥接服务
screen -dmS claude-bridge python3 -u claude_bridge.py
```

---

## 运维监控

* 查看实时运行日志：
  * Codex 日志：`tail -f logs/codex_bridge.log`
  * AGY 日志：`tail -f logs/agy_bridge.log`
  * Claude 日志：`tail -f logs/claude_bridge.log`
* 进入后台 screen 视窗：
  * `screen -r codex-bridge`
  * `screen -r agy-bridge`
  * `screen -r claude-bridge`

---

## 开源协议

本项目采用 MIT 协议开源。
