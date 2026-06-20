# 🤖 Agent QQ Bridge

QQ 官方机器人网关直连 AI Agent 的独立桥接通道。本项目支持 **Claude Code (Anthropic)** 和 **AGY (Google Antigravity / Gemini)**。

一个 QQ 号可以挂载多个机器人，各通道独立运行，通过 WebSocket 直连腾讯官方网关，将 QQ 消息转发给服务器中的 Agent，并将 Agent 执行后的结果与代码产出自动发回 QQ。

---

## 🏗️ 架构设计

```
QQ 用户发送指令
     ↓
腾讯官方 WebSocket 网关 (api.sgroup.qq.com)
     ↓
[agy_bridge.py / claude_bridge.py] (本桥接器)
     ↓  非交互式执行 (subprocess)
AI Agent CLI (AGY / Claude Code)
     ↓
结果返回 ➡️ QQ 消息接口
```

---

## ✨ 核心特性

* **双 Agent 引擎支持**：
  * **Claude Code**：运行 [claude_bridge.py](file:///root/claude-qq-bridge/claude_bridge.py)，支持基于 `--allowedTools` 白名单的无阻塞推理与执行。
  * **AGY (Google Antigravity)**：运行 [agy_bridge.py](file:///root/claude-qq-bridge/agy_bridge.py)，支持基于 `--dangerously-skip-permissions` 的完全静默、无阻塞终端命令与代码编写。
* **零外部依赖**：仅使用 `aiohttp` + `httpx`，无需安装 NapCatQQ、go-cqhttp 等第三方非官方客户端，安全合规，防封控。
* **多轮会话记忆**：内置 `SESSION_STORE` 会话缓存机制，按用户的 `user_openid` 隔离并维护最多 200 条（100 轮）的连续对话历史。
* **安全隔离防御**：严格限制仅允许配置的 `MASTER_OPENID`（主理人）进行指令交互，防止其他人误用或恶意调用系统命令。
* **紧急刹车机制**：
  * 支持在 QQ 中发送 `/stop`、`/停止`、`/kill` 或 `杠stop` 强杀指令。
  * 桥接器会立即在进程级别终止后台运行的 Agent 子进程，并取消协程任务。
  * 内置忙碌防并发冲突保护，避免同一个工作区被并发指令篡改。
* **便捷会话控制**：支持 `/clear`、`/new`（清空上下文，开启新会话）。

---

## 📋 前置条件

1. **QQ 官方机器人**：在 [QQ 开放平台](https://q.qq.com) 注册机器人，获取 `AppID` 和 `Token (AppSecret)`。
2. **AI Agent CLI**：
   * 安装 Anthropic 官方的 `claude` 命令行工具。
   * 安装 Google Antigravity 的 `agy` 命令行工具。
3. **环境依赖**：Python 3.10+，以及 `aiohttp`、`httpx` 库。

---

## 🚀 安装与部署

### 1. 克隆与安装依赖
```bash
git clone https://github.com/zz327455573/claude-qq-bridge.git
cd claude-qq-bridge
pip install aiohttp httpx
```

### 2. 配置说明
分别编辑 [claude_bridge.py](file:///root/claude-qq-bridge/claude_bridge.py) 或 [agy_bridge.py](file:///root/claude-qq-bridge/agy_bridge.py) 顶部的配置区：
```python
# QQ 开放平台机器人凭证
APP_ID = "你的AppID"
APP_SECRET = "你的AppSecret"

# 主理人标识（用户的 openid，可以在运行后发送消息查看日志获取）
MASTER_OPENID = "你的QQ_openid"
```

### 3. 启动运行
建议使用 `screen` 在后台挂载运行：

* **运行 AGY 桥接服务**：
  ```bash
  # 针对部分虚拟机/容器环境，可配置专用的 SCREENDIR
  mkdir -p ~/.screen && chmod 700 ~/.screen
  export SCREENDIR=$HOME/.screen
  
  # 后台启动
  screen -dmS agy-bridge python3 -u agy_bridge.py
  ```
* **运行 Claude Code 桥接服务**：
  ```bash
  screen -dmS claude-bridge python3 -u claude_bridge.py
  ```

---

## 📡 运维监控

* **查看实时运行日志**：
  * AGY 日志：`tail -f logs/agy_bridge.log`
  * Claude 日志：`tail -f logs/claude_bridge.log`
* **进入后台 screen 视窗**：
  * `export SCREENDIR=$HOME/.screen && screen -r agy-bridge`
  * `screen -r claude-bridge`

---

## 📜 开源协议

本项目采用 MIT 协议开源。
