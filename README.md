# claude-qq-bridge

QQ 官方机器人网关 ↔ Claude Code CLI 的桥接器。

一个 QQ 号挂多个机器人，本桥接器独立占用一个机器人通道（AppID: 你的AppID），通过 WebSocket 直连腾讯官方网关，把 QQ 消息转发给 Claude Code，再把 Claude Code 的回复发回 QQ。

## 架构

```
QQ 用户发消息
    ↓
腾讯官方 WS 网关 (api.sgroup.qq.com)
    ↓
claude_bridge.py (本程序)
    ↓  subprocess.run(["claude", "--print", ...])
Claude Code CLI (Anthropic)
    ↓
结果返回 → QQ
```

## 特性

- **零依赖**：只用 `aiohttp` + `httpx`，不依赖 NapCatQQ / go-cqhttp
- **官方通道**：直连腾讯官方 WebSocket 网关，防风控
- **多轮对话**：按 user_openid 存储会话历史（最多 200 条）
- **安全隔离**：只有 MASTER_OPENID 指定的用户可操控
- **独立进程**：一个机器人一个进程，互不干扰
- **支持命令**：`/clear` 清空历史，`/new` 新会话

## 前置条件

1. **QQ 官方机器人**：在 [QQ 开放平台](https://q.qq.com) 注册机器人，获取 AppID 和 Token
2. **Claude Code CLI**：安装 Anthropic 官方 CLI 工具
3. **Python 3.10+**
4. **aiohttp**、**httpx**

## 安装

```bash
# 克隆
git clone https://github.com/zz327455573/claude-qq-bridge.git
cd claude-qq-bridge

# 依赖
pip install aiohttp httpx
```

## 配置

编辑 `claude_bridge.py` 顶部配置区：

```python
APP_ID = "你的AppID"
APP_SECRET = "你的AppSecret"
MASTER_OPENID = "你的QQ_openid"  # 不是QQ号！是openid
```

**获取 openid**：让目标用户给机器人发一条消息，日志里会自动打印。

## 运行

```bash
# 前台
python -u claude_bridge.py

# 后台（screen）
screen -dmS claude-bridge python -u claude_bridge.py

# 查看日志
screen -r claude-bridge
# 或
tail -f claude_bridge.log
```

## 多机器人共用一个 QQ 号

一个 QQ 号可以挂多个机器人，每个机器人独立运行一个 `claude_bridge.py` 实例，只需：
- 每个实例用不同的 AppID/Token
- 每个实例监听不同的 QQ 用户

## 典型场景

- 个人 AI 助手通道
- 技术问答机器人
- 远程运维指令通道

## 开源协议

MIT
