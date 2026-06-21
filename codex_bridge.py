#!/usr/bin/env python3
"""
codex_bridge.py — QQ 官方 WebSocket 网关直连 Codex
架构: QQ官方WS网关 ↔ Python asyncio ↔ subprocess(codex exec --json / codex exec resume --json)

利用 Codex 原生的 thread_id + --json 输出实现会话续接。
每个 QQ 用户独立一个 Codex 会话，通过 thread_id 续接。

依赖: pip install aiohttp httpx
启动: python3 /root/codex_bridge.py
"""
import asyncio
import json
import subprocess
import re
import os
import sys
import time
import uuid
import logging
from typing import Optional, Dict, Any

# ================= 配置区 =================
# QQ 开放平台机器人凭证（可在此配置单独的机器人凭证，默认复用 Claude 相同的凭证）
APP_ID = "1903831548"
APP_SECRET = "lgcYVTRQPPPQSUXaeintz6DLTcmw7IUg"

# 主理人标识（权限隔离用，对应用户的 openid）
MASTER_OPENID = "22CEA207255FDA723AC1EB4FDA9D09EF"

# Codex 运行超时限制（秒）
CODEX_TIMEOUT = 600

# API 端点
API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL_PATH = "/gateway"

# 连接参数
CONNECT_TIMEOUT = 20
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_RECONNECT_ATTEMPTS = 100
HEARTBEAT_INTERVAL = 15.0  # 服务端 33 秒超时，留足余量
# ==========================================

# 创建日志目录
os.makedirs("/root/logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/root/logs/codex_bridge.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("codex_bridge")

# ANSI 颜色渲染代码清洗正则
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def clean_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub('', text).strip()


# ================= 全局状态变量 =================
_access_token: Optional[str] = None
_token_expires_at: float = 0.0
_session_id: Optional[str] = None
_last_seq: Optional[int] = None
_ws = None
_http_client = None
_running = False
_last_msg_id: Optional[str] = None
_ws_session = None
heartbeat_task = None
_active_proc: Optional[asyncio.subprocess.Process] = None
_active_task: Optional[asyncio.Task] = None


# 用户 thread_id 映射（user_openid -> codex thread_id）
THREAD_MAPPING_FILE = "/root/codex_user_threads.json"

def load_thread_mapping() -> dict:
    if os.path.exists(THREAD_MAPPING_FILE):
        try:
            with open(THREAD_MAPPING_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_thread_mapping(mapping: dict):
    with open(THREAD_MAPPING_FILE, "w") as f:
        json.dump(mapping, f, indent=2)

def get_thread_id_for_user(user_id: str) -> Optional[str]:
    mapping = load_thread_mapping()
    return mapping.get(user_id)

def save_thread_id_for_user(user_id: str, thread_id: str):
    mapping = load_thread_mapping()
    mapping[user_id] = thread_id
    save_thread_mapping(mapping)

def extract_thread_id_from_json(output: str) -> Optional[str]:
    for line in output.split(chr(10)):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("type") == "thread.started":
                tid = data.get("thread_id")
                if tid:
                    return tid
        except json.JSONDecodeError:
            continue
    return None

def extract_reply_from_json(output: str) -> str:
    last_message = ""
    for line in output.split(chr(10)):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("type") == "item.completed":
                item = data.get("item", {})
                if item.get("type") == "agent_message":
                    last_message = item.get("text", "")
        except json.JSONDecodeError:
            continue
    return last_message


async def query_codex(user_id: str, prompt: str) -> str:
    """Execute codex for user, using --json and resume for session continuation"""

    # /clear command interception
    if prompt.strip() in ["/clear", "/清空", "/新对话", "/new", "/reset"]:
        mapping = load_thread_mapping()
        if user_id in mapping:
            del mapping[user_id]
            save_thread_mapping(mapping)
        return "🧹 记忆已清空，开始新对话！"

    thread_id = get_thread_id_for_user(user_id)

    if thread_id:
        cmd = ["codex", "exec", "resume", thread_id, prompt,
               "--dangerously-bypass-approvals-and-sandbox", "--json"]
        logger.info(f"续接线程 {thread_id} for user {user_id}")
    else:
        cmd = ["codex", "exec", prompt,
               "--dangerously-bypass-approvals-and-sandbox", "--json"]
        logger.info(f"为用户创建新会话 {user_id}")

    try:
        global _active_proc
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HTTPS_PROXY": "", "HTTP_PROXY": ""},
        )
        _active_proc = proc
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=CODEX_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "[Codex timeout]"
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        finally:
            if _active_proc == proc:
                _active_proc = None

        output = stdout.decode("utf-8", errors="replace").strip()

        # Extract new thread_id from JSON output (for first-time creation)
        new_thread_id = extract_thread_id_from_json(output)
        if new_thread_id and not thread_id:
            save_thread_id_for_user(user_id, new_thread_id)
            logger.info(f"新线程 {new_thread_id} 已保存给用户 {user_id}")

        # Extract final agent message from JSON
        reply = extract_reply_from_json(output)

        if not reply:
            if stderr:
                err_text = stderr.decode("utf-8", errors="replace").strip()
                return f"[Codex error] {err_text[:300]}"
            return "[Codex no output]"

        return reply

    except Exception as e:
        return f"[Codex error] {str(e)[:300]}"




# ================= HTTP Client =================

def get_http_client():
    global _http_client
    if _http_client is None:
        import httpx
        _http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    return _http_client


# ================= Token 管理 =================

async def ensure_token() -> str:
    global _access_token, _token_expires_at
    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token

    client = get_http_client()
    resp = await client.post(
        TOKEN_URL,
        json={"appId": APP_ID, "clientSecret": APP_SECRET},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()

    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Failed to get token: {data}")

    expires_in = int(data.get("expires_in", 7200))
    _access_token = token
    _token_expires_at = time.time() + expires_in
    logger.info(f"Token 已刷新, expires in {expires_in}s")
    return token


async def get_gateway_url() -> str:
    token = await ensure_token()
    client = get_http_client()
    resp = await client.get(
        f"{API_BASE}{GATEWAY_URL_PATH}",
        headers={
            "Authorization": f"QQBot {token}",
            "User-Agent": "AgyBridge/2.0",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    url = data.get("url")
    if not url:
        raise RuntimeError(f"获取网关地址失败: {data}")
    return url


# ================= WebSocket 通信 =================

async def send_identify(ws):
    """op 2 Identify"""
    token = await ensure_token()
    payload = {
        "op": 2,
        "d": {
            "token": f"QQBot {token}",
            "intents": (1 << 25) | (1 << 30) | (1 << 12) | (1 << 26),
            "shard": [0, 1],
            "properties": {
                "$os": "Linux",
                "$browser": "agy-bridge",
                "$device": "agy-bridge",
            },
        },
    }
    await ws.send_json(payload)
    logger.info("身份认证已发送")


async def send_resume(ws):
    """op 6 Resume"""
    token = await ensure_token()
    payload = {
        "op": 6,
        "d": {
            "token": f"QQBot {token}",
            "session_id": _session_id,
            "seq": _last_seq,
        },
    }
    await ws.send_json(payload)
    logger.info(f"Resume sent (session={_session_id}, seq={_last_seq})")


async def send_heartbeat(ws):
    """op 1 Heartbeat"""
    await ws.send_json({"op": 1, "d": _last_seq})


def _next_msg_seq(msg_id: str = "default") -> int:
    time_part = int(time.time()) % 100000000
    rand = int(uuid.uuid4().hex[:4], 16)
    return (time_part ^ rand) % 65536


async def send_message_rest(user_openid: str, content: str) -> bool:
    """通过 REST API 发送 C2C 消息"""
    token = await ensure_token()
    client = get_http_client()
    headers = {
        "Authorization": f"QQBot {token}",
        "Content-Type": "application/json",
        "User-Agent": "AgyBridge/2.0",
    }
    msg_seq = _next_msg_seq(user_openid)
    
    # 消息长度限制控制
    display_content = content
    if len(content) > 4000:
        display_content = content[:3990] + "\n\n... (已截断)"
    body = {
        "markdown": {"content": display_content},
        "msg_type": 2,
        "msg_seq": msg_seq,
    }
    try:
        resp = await client.post(
            f"{API_BASE}/v2/users/{user_openid}/messages",
            headers=headers,
            json=body,
            timeout=30.0,
        )
        if resp.status_code >= 400:
            logger.error(f"Send failed [{resp.status_code}]: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Send exception: {e}")
        return False


# ================= 消息去重处理 =================

_seen_messages: Dict[str, float] = {}


def is_duplicate(msg_id: str) -> bool:
    now = time.time()
    if msg_id in _seen_messages and now - _seen_messages[msg_id] < 300:
        return True
    _seen_messages[msg_id] = now
    # 垃圾清理
    if len(_seen_messages) > 1000:
        for k in list(_seen_messages.keys()):
            if now - _seen_messages[k] > 600:
                del _seen_messages[k]
    return False


async def handle_c2c_message(d: dict):
    """处理 C2C_MESSAGE_CREATE"""
    global _last_msg_id, _active_proc, _active_task

    msg_id = str(d.get("id", ""))
    if not msg_id or is_duplicate(msg_id):
        return

    content = str(d.get("content", "")).strip()
    author = d.get("author") if isinstance(d.get("author"), dict) else {}
    user_openid = str(author.get("user_openid", ""))

    if not user_openid or not content:
        return

    _last_msg_id = msg_id
    logger.info(f"[收到] 用户={user_openid}: {content[:100]}")

    # 权限隔离，仅限主理人操控
    if user_openid != MASTER_OPENID:
        logger.info(f"[跳过] 非主理人: {user_openid}")
        return

    # /stop 紧急强制终止指令拦截
    if content.strip().lower() in ["/stop", "/停止", "/kill", "杠stop", "-stop", "--stop"]:
        logger.info("[Recv] 收到停止指令")
        killed = False
        if _active_proc and _active_proc.returncode is None:
            try:
                _active_proc.kill()
                killed = True
            except Exception as e:
                logger.error(f"终止进程失败: {e}")
        
        if _active_task and not _active_task.done():
            _active_task.cancel()
            killed = True
            
        _active_proc = None
        _active_task = None
        
        reply = "🛑 已强制停止当前正在运行的 Codex 任务！" if killed else "ℹ️ 当前没有正在运行的 Codex 任务。"
        await send_message_rest(user_openid, reply)
        return

    # 忙碌状态排斥检查，防止同一个 workspace 被并发修改
    if _active_proc and _active_proc.returncode is None:
        await send_message_rest(user_openid, "⚠️ 当前已有任务正在执行中，请稍候。若需强行终止，请发送 `/stop`。")
        return

    # 调用 Codex 并转发回复
    logger.info(f"[QQ -> Codex] {content}")
    
    current_task = asyncio.current_task()
    _active_task = current_task
    
    try:
        reply = await query_codex(user_openid, content)
    finally:
        if _active_task == current_task:
            _active_task = None
            
    logger.info(f"[Codex -> QQ] {reply[:200]}")

    success = await send_message_rest(user_openid, reply)
    if not success:
        logger.error("Reply send failed")


# ================= 心跳发送 =================

async def _heartbeat_sender(ws, interval: float):
    """定期发送 op 1 Heartbeat 防止超时断连"""
    try:
        while _running and ws and not ws.closed:
            await asyncio.sleep(interval)
            if ws and not ws.closed:
                await ws.send_json({"op": 1, "d": _last_seq})
                logger.debug("Heartbeat sent")
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.debug(f"Heartbeat error: {e}")


# ================= 主事件循环 =================

async def event_loop(ws):
    """WebSocket 事件监听与自动重连循环"""
    global _session_id, _last_seq, _running, _ws, heartbeat_task, _ws_session
    _ws = ws
    backoff_idx = 0
    connect_time = 0.0
    quick_disconnect_count = 0
    heartbeat_interval = HEARTBEAT_INTERVAL
    heartbeat_task = None

    # 启动心跳任务
    heartbeat_task = asyncio.create_task(_heartbeat_sender(ws, heartbeat_interval))

    while _running:
        try:
            connect_time = time.monotonic()

            # 数据读取循环
            while _running and ws and not ws.closed:
                msg = await ws.receive()

                if msg.type == 1:  # TEXT
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning(f"JSON parse error: {msg.data[:100]}")
                        continue

                    op = payload.get("op")
                    t = payload.get("t")
                    s = payload.get("s")
                    d = payload.get("d")

                    if isinstance(s, int) and (_last_seq is None or s > _last_seq):
                        _last_seq = s

                    # op 10 Hello
                    if op == 10:
                        d_data = d if isinstance(d, dict) else {}
                        interval_ms = d_data.get("heartbeat_interval", 30000)
                        heartbeat_interval = interval_ms / 1000.0 * 0.8
                        logger.info(f"心跳参数收到, heartbeat={heartbeat_interval:.1f}s")
                        if _session_id and _last_seq is not None:
                            await send_resume(ws)
                        else:
                            await send_identify(ws)
                        continue

                    # op 0 Dispatch
                    if op == 0 and t:
                        if t == "READY":
                            if isinstance(d, dict):
                                _session_id = d.get("session_id")
                                logger.info(f"就绪, 会话ID={_session_id}")
                        elif t == "RESUMED":
                            logger.info("Session resumed")
                        elif t == "C2C_MESSAGE_CREATE":
                            task = asyncio.create_task(handle_c2c_message(d))
                            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                        else:
                            logger.debug(f"Unhandled event: {t}")
                        continue

                    # op 11 Heartbeat ACK
                    if op == 11:
                        continue

                    # op 7 Server Reconnect
                    if op == 7:
                        logger.info("Server requests reconnect")
                        break

                    # op 9 Invalid Session
                    if op == 9:
                        resumable = bool(d) if d is not None else False
                        if not resumable:
                            _session_id = None
                            _last_seq = None
                        logger.info(f"Invalid session (resumable={resumable})")
                        break

                    logger.debug(f"Unknown op: {op}")

                elif msg.type == 4:  # CLOSE
                    raise Exception(f"WS closed: code={msg.data} extra={msg.extra}")
                elif msg.type in (5, 6):  # CLOSED / ERROR
                    raise Exception("WS connection lost")

            if not _running:
                return

            # 断线频发防御
            duration = time.monotonic() - connect_time
            if duration < 5.0 and connect_time > 0:
                quick_disconnect_count += 1
                if quick_disconnect_count >= 3:
                    logger.error("Too many quick disconnects, check AppID/Secret/permissions")
                    _running = False
                    return
            else:
                quick_disconnect_count = 0

            # 避让重连
            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info(f"重连中，等待 {delay}s (attempt {backoff_idx + 1})")
            await asyncio.sleep(delay)

            try:
                if _ws_session and not _ws_session.closed:
                    await _ws_session.close()
                gateway_url = await get_gateway_url()
                import aiohttp
                _ws_session = aiohttp.ClientSession(trust_env=True)
                ws = await _ws_session.ws_connect(
                    gateway_url,
                    headers={"User-Agent": "AgyBridge/2.0"},
                    timeout=aiohttp.ClientWSTimeout(ws_close=CONNECT_TIMEOUT),
                )
                _ws = ws
                backoff_idx = 0
                quick_disconnect_count = 0
                if heartbeat_task and not heartbeat_task.done():
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
                heartbeat_task = asyncio.create_task(_heartbeat_sender(ws, heartbeat_interval))
                logger.info("重连成功")
            except Exception as e:
                logger.error(f"Reconnect failed: {e}")
                backoff_idx += 1
                if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                    logger.error("达到最大重连次数")
                    _running = False
                    return

        except asyncio.CancelledError:
            return
        except Exception as e:
            if not _running:
                return
            logger.error(f"事件循环错误: {e}")
            await asyncio.sleep(2)


# ================= 入口 =================

async def main():
    global _running, _http_client, _ws_session

    print("=" * 50)
    print("  Codex <-> QQ Bot (Official WebSocket)")
    print(f"  AppID: {APP_ID}")
    print(f"  Master: {MASTER_OPENID}")
    print("=" * 50)

    # 启动时加载持久化的用户会话映射关系
    # thread mapping loaded on demand

    import httpx
    import aiohttp

    _http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    _running = True

    # 首次连接
    gateway_url = await get_gateway_url()
    logger.info(f"网关地址: {gateway_url}")

    _ws_session = aiohttp.ClientSession(trust_env=True)
    ws = await _ws_session.ws_connect(
        gateway_url,
        headers={"User-Agent": "AgyBridge/2.0"},
        timeout=aiohttp.ClientWSTimeout(ws_close=CONNECT_TIMEOUT),
    )
    logger.info("WebSocket 已连接")

    try:
        await event_loop(ws)
    except KeyboardInterrupt:
        logger.info("用户中断")
    finally:
        _running = False
        try:
            await ws.close()
            await _ws_session.close()
        except Exception:
            pass
        await _http_client.aclose()
        _http_client = None
        logger.info("已关闭")


if __name__ == "__main__":
    asyncio.run(main())
