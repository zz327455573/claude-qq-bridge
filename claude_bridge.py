#!/usr/bin/env python3
"""
claude-qq-bridge.py — QQ 官方 WebSocket 网关直连 Claude Code
架构: QQ官方WS网关 ↔ Python asyncio ↔ subprocess(claude --print)

支持 C2C 私聊 + 群聊（@提及触发 + 上下文缓存回填）

依赖: pip install aiohttp httpx
启动: python3 /root/claude-qq-bridge.py
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
APP_ID = "你的AppID"
APP_SECRET = "你的AppSecret"
MASTER_OPENID = "你的MASTER_OPENID"
CLAUDE_TIMEOUT = 600

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL_PATH = "/gateway"

CONNECT_TIMEOUT = 20
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_RECONNECT_ATTEMPTS = 100
HEARTBEAT_INTERVAL = 15.0
# ==========================================

os.makedirs("/root/logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/root/logs/claude-qq-bridge.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("claude_qq_bridge")

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def clean_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub('', text).strip()

# 会话存储
SESSION_STORE: dict[str, list] = {}
MAX_HISTORY_LEN = 200
GROUP_CONTEXT_CACHE: dict[str, list[str]] = {}

# 全局状态
_access_token: Optional[str] = None
_token_expires_at: float = 0.0
_session_id: Optional[str] = None
_last_seq: Optional[int] = None
_ws = None
_http_client = None
_running = False
_last_msg_id: Optional[str] = None
_bot_openid: str = ""
_ws_session = None
heartbeat_task = None
_active_proc: Optional[asyncio.subprocess.Process] = None
_active_task: Optional[asyncio.Task] = None


async def query_claude(user_id: str, prompt: str) -> str:
    """调用 claude --print，带多轮会话记忆"""
    if user_id not in SESSION_STORE:
        SESSION_STORE[user_id] = []

    if prompt.strip().lower() in ["/clear", "/清空", "/新对话", "/new", "/reset"]:
        SESSION_STORE[user_id] = []
        return "🧹 记忆已清空，开始新对话！"

    history = SESSION_STORE[user_id]
    parts = []
    for msg in history:
        role = "Human" if msg["role"] == "user" else "Assistant"
        parts.append(f"{role}: {msg['content']}")
    parts.append(f"Human: {prompt}")
    parts.append("Assistant:")
    final_prompt = "\n\n".join(parts)

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "--print", "--allowedTools",
            "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Agent,TodoRead,TodoWrite,wenda-ops:fetch_url,wenda-ops:server_audit,wenda-ops:content_creator,wenda-ops:web_search,wenda-ops:get_weather,wenda-ops:tavily_search,wenda-ops:exa_search",
            "--", final_prompt,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HTTPS_PROXY": "", "HTTP_PROXY": ""},
        )
        global _active_proc
        _active_proc = proc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "[Claude timeout]"
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        finally:
            if _active_proc == proc:
                _active_proc = None

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            if stderr:
                return f"[Claude error] {stderr.decode('utf-8', errors='replace').strip()[:300]}"
            return "[Claude no output]"

        reply = clean_ansi(output)

        SESSION_STORE[user_id].append({"role": "user", "content": prompt})
        SESSION_STORE[user_id].append({"role": "assistant", "content": reply})
        if len(SESSION_STORE[user_id]) > MAX_HISTORY_LEN:
            SESSION_STORE[user_id] = SESSION_STORE[user_id][-MAX_HISTORY_LEN:]

        return reply
    except Exception as e:
        return f"[Claude error] {str(e)[:300]}"


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
    logger.info(f"Token refreshed, expires in {expires_in}s")
    return token


async def get_gateway_url() -> str:
    token = await ensure_token()
    client = get_http_client()
    resp = await client.get(
        f"{API_BASE}{GATEWAY_URL_PATH}",
        headers={"Authorization": f"QQBot {token}", "User-Agent": "ClaudeBridge/2.0"},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    url = data.get("url")
    if not url:
        raise RuntimeError(f"Failed to get gateway URL: {data}")
    return url


# ================= WebSocket 通信 =================

async def send_identify(ws):
    token = await ensure_token()
    payload = {
        "op": 2,
        "d": {
            "token": f"QQBot {token}",
            "intents": (1 << 25) | (1 << 30) | (1 << 12) | (1 << 26),
            "shard": [0, 1],
            "properties": {"$os": "Linux", "$browser": "claude-qq-bridge", "$device": "claude-qq-bridge"},
        },
    }
    await ws.send_json(payload)
    logger.info("Identify sent")


async def send_resume(ws):
    token = await ensure_token()
    payload = {
        "op": 6,
        "d": {"token": f"QQBot {token}", "session_id": _session_id, "seq": _last_seq},
    }
    await ws.send_json(payload)
    logger.info(f"Resume sent (session={_session_id}, seq={_last_seq})")


def _next_msg_seq(msg_id: str = "default") -> int:
    time_part = int(time.time()) % 100000000
    rand = int(uuid.uuid4().hex[:4], 16)
    return (time_part ^ rand) % 65536


async def send_message_rest(user_openid: str, content: str) -> bool:
    """发送 C2C 私聊消息"""
    token = await ensure_token()
    client = get_http_client()
    headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json", "User-Agent": "ClaudeBridge/2.0"}
    msg_seq = _next_msg_seq(user_openid)
    display_content = content[:3990] + "\n\n... (已截断)" if len(content) > 4000 else content
    body = {"markdown": {"content": display_content}, "msg_type": 2, "msg_seq": msg_seq}
    try:
        resp = await client.post(f"{API_BASE}/v2/users/{user_openid}/messages", headers=headers, json=body, timeout=30.0)
        if resp.status_code >= 400:
            logger.error(f"Send failed [{resp.status_code}]: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Send exception: {e}")
        return False


async def send_group_message_rest(group_openid: str, content: str, reply_to: Optional[str] = None) -> bool:
    """发送群聊消息"""
    token = await ensure_token()
    client = get_http_client()
    headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json", "User-Agent": "ClaudeBridge/2.0"}
    msg_seq = _next_msg_seq(group_openid)
    display_content = content[:3990] + "\n\n... (已截断)" if len(content) > 4000 else content
    body = {"markdown": {"content": display_content}, "msg_type": 2, "msg_seq": msg_seq}
    if reply_to:
        body["msg_id"] = reply_to
    try:
        resp = await client.post(f"{API_BASE}/v2/groups/{group_openid}/messages", headers=headers, json=body, timeout=30.0)
        if resp.status_code >= 400:
            logger.error(f"Group send failed [{resp.status_code}]: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Group send exception: {e}")
        return False


# ================= 消息去重 =================

_seen_messages: Dict[str, float] = {}

def is_duplicate(msg_id: str) -> bool:
    now = time.time()
    if msg_id in _seen_messages and now - _seen_messages[msg_id] < 300:
        return True
    _seen_messages[msg_id] = now
    if len(_seen_messages) > 1000:
        for k in list(_seen_messages.keys()):
            if now - _seen_messages[k] > 600:
                del _seen_messages[k]
    return False


# ================= 群聊消息处理 =================

async def handle_group_message(d: dict, event_type: str):
    """处理群聊消息事件（GROUP_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE）"""
    global _last_msg_id, _active_proc, _active_task, _bot_openid, GROUP_CONTEXT_CACHE

    msg_id = str(d.get("id", ""))
    logger.info(f"[Group Raw] event={event_type} msg_id={msg_id} group={d.get('group_openid')} content={d.get('content')} mentions={d.get('mentions')}")
    if not msg_id or is_duplicate(msg_id):
        return

    group_openid = str(d.get("group_openid", ""))
    content = str(d.get("content", "")).strip()
    author = d.get("author") if isinstance(d.get("author"), dict) else {}
    member_openid = str(author.get("member_openid", ""))

    if not group_openid or not content:
        return

    sender_name = author.get("nickname") or author.get("username")
    if not sender_name:
        sender_name = f"user_{member_openid[-6:]}" if member_openid else "User"
    msg_line = f"[{sender_name}] {content.strip()}"

    # @ 提及校验
    is_mentioned = False
    my_openid_in_group = ""

    mentions = d.get("mentions") or []
    for m in mentions:
        if m.get("is_you") is True:
            my_openid_in_group = m.get("member_openid") or m.get("id") or m.get("user_openid") or ""
            break

    if event_type == "GROUP_AT_MESSAGE_CREATE":
        is_mentioned = True
    elif event_type == "GROUP_MESSAGE_CREATE":
        if my_openid_in_group:
            is_mentioned = True
        elif _bot_openid and mentions:
            for m in mentions:
                mid = m.get("member_openid") or m.get("id") or m.get("user_openid") or ""
                if str(mid) == str(_bot_openid):
                    is_mentioned = True
                    break

    if not is_mentioned:
        if group_openid not in GROUP_CONTEXT_CACHE:
            GROUP_CONTEXT_CACHE[group_openid] = []
        GROUP_CONTEXT_CACHE[group_openid].append(msg_line)
        GROUP_CONTEXT_CACHE[group_openid] = GROUP_CONTEXT_CACHE[group_openid][-100:]
        return

    _last_msg_id = msg_id
    logger.info(f"[Group Recv] group={group_openid} member={member_openid}: {content[:100]}")

    # 权限隔离
    if member_openid != MASTER_OPENID:
        logger.info(f"[Group Skip] non-master openid: {member_openid}")
        if group_openid not in GROUP_CONTEXT_CACHE:
            GROUP_CONTEXT_CACHE[group_openid] = []
        GROUP_CONTEXT_CACHE[group_openid].append(msg_line)
        GROUP_CONTEXT_CACHE[group_openid] = GROUP_CONTEXT_CACHE[group_openid][-100:]
        return

    # 提取缓存的群聊上下文
    channel_context = ""
    if group_openid in GROUP_CONTEXT_CACHE:
        history_lines = GROUP_CONTEXT_CACHE[group_openid]
        if history_lines:
            channel_context = "[Recent group chat context]\n" + "\n".join(history_lines)
            GROUP_CONTEXT_CACHE[group_openid] = []

    # 清洗 @ 提及标签
    cleaned_content = content
    if my_openid_in_group:
        cleaned_content = re.sub(rf"<@!?{my_openid_in_group}>", "", cleaned_content).strip()
    if _bot_openid:
        cleaned_content = re.sub(rf"<@!?{_bot_openid}>", "", cleaned_content).strip()

    if not cleaned_content:
        return

    # /stop 指令
    if cleaned_content.lower() in ["/stop", "/停止", "/kill", "杠stop", "-stop", "--stop"]:
        logger.info("[Group Recv] Stop command received")
        killed = False
        if _active_proc and _active_proc.returncode is None:
            try:
                _active_proc.kill()
                killed = True
            except Exception as e:
                logger.error(f"Failed to kill active process: {e}")
        if _active_task and not _active_task.done():
            _active_task.cancel()
            killed = True
        _active_proc = None
        _active_task = None
        reply = "🛑 已强制停止当前任务！" if killed else "ℹ️ 当前没有正在运行的任务。"
        await send_group_message_rest(group_openid, reply, reply_to=msg_id)
        return

    # 忙碌检查
    if _active_proc and _active_proc.returncode is None:
        await send_group_message_rest(group_openid, "⚠️ 当前已有任务正在执行中，请稍候。", reply_to=msg_id)
        return

    # 拼装 prompt
    prompt_to_send = cleaned_content
    if channel_context:
        prompt_to_send = f"{channel_context}\n\n[New message]\n{cleaned_content}"

    logger.info(f"[QQ Group -> Claude] group={group_openid}: {cleaned_content}")

    current_task = asyncio.current_task()
    _active_task = current_task
    try:
        reply = await query_claude(group_openid, prompt_to_send)
    finally:
        if _active_task == current_task:
            _active_task = None

    logger.info(f"[Claude -> QQ Group] {reply[:200]}")
    success = await send_group_message_rest(group_openid, reply, reply_to=msg_id)
    if not success:
        logger.error("Group reply send failed")


# ================= C2C 私聊处理 =================

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
    logger.info(f"[Recv] openid={user_openid}: {content[:100]}")

    if user_openid != MASTER_OPENID:
        logger.info(f"[Skip] non-master openid: {user_openid}")
        return

    # /stop 指令
    if content.strip().lower() in ["/stop", "/停止", "/kill", "杠stop", "-stop", "--stop"]:
        logger.info("[Recv] Stop command received")
        killed = False
        if _active_proc and _active_proc.returncode is None:
            try:
                _active_proc.kill()
                killed = True
            except Exception as e:
                logger.error(f"Failed to kill active process: {e}")
        if _active_task and not _active_task.done():
            _active_task.cancel()
            killed = True
        _active_proc = None
        _active_task = None
        reply = "🛑 已强制停止当前任务！" if killed else "ℹ️ 当前没有正在运行的任务。"
        await send_message_rest(user_openid, reply)
        return

    if _active_proc and _active_proc.returncode is None:
        await send_message_rest(user_openid, "⚠️ 当前已有任务正在执行中，请稍候。")
        return

    logger.info(f"[QQ -> Claude] {content}")

    current_task = asyncio.current_task()
    _active_task = current_task
    try:
        reply = await query_claude(user_openid, content)
    finally:
        if _active_task == current_task:
            _active_task = None

    logger.info(f"[Claude -> QQ] {reply[:200]}")
    success = await send_message_rest(user_openid, reply)
    if not success:
        logger.error("Reply send failed")


# ================= 心跳 =================

async def _heartbeat_sender(ws, interval: float):
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
    global _session_id, _last_seq, _running, _ws, heartbeat_task, _ws_session
    _ws = ws
    backoff_idx = 0
    connect_time = 0.0
    quick_disconnect_count = 0
    heartbeat_interval = HEARTBEAT_INTERVAL
    heartbeat_task = None

    heartbeat_task = asyncio.create_task(_heartbeat_sender(ws, heartbeat_interval))

    while _running:
        try:
            connect_time = time.monotonic()

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

                    if op == 10:
                        d_data = d if isinstance(d, dict) else {}
                        interval_ms = d_data.get("heartbeat_interval", 30000)
                        heartbeat_interval = interval_ms / 1000.0 * 0.8
                        logger.info(f"Hello recv, heartbeat={heartbeat_interval:.1f}s")
                        if _session_id and _last_seq is not None:
                            await send_resume(ws)
                        else:
                            await send_identify(ws)
                        continue

                    if op == 0 and t:
                        logger.info(f"[WS Dispatch] event_type={t}")
                        if t == "READY":
                            if isinstance(d, dict):
                                global _bot_openid
                                _session_id = d.get("session_id")
                                user = d.get("user") if isinstance(d.get("user"), dict) else {}
                                _bot_openid = str(user.get("id", ""))
                                logger.info(f"READY, session_id={_session_id}, bot_openid={_bot_openid}")
                        elif t == "RESUMED":
                            logger.info("Session resumed")
                        elif t == "C2C_MESSAGE_CREATE":
                            task = asyncio.create_task(handle_c2c_message(d))
                            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                        elif t in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
                            task = asyncio.create_task(handle_group_message(d, t))
                            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                        else:
                            logger.debug(f"Unhandled event: {t}")
                        continue

                    if op == 11:
                        continue
                    if op == 7:
                        logger.info("Server requests reconnect")
                        break
                    if op == 9:
                        resumable = bool(d) if d is not None else False
                        if not resumable:
                            _session_id = None
                            _last_seq = None
                        logger.info(f"Invalid session (resumable={resumable})")
                        break

                    logger.debug(f"Unknown op: {op}")

                elif msg.type == 4:
                    raise Exception(f"WS closed: code={msg.data} extra={msg.extra}")
                elif msg.type in (5, 6):
                    raise Exception("WS connection lost")

            if not _running:
                return

            duration = time.monotonic() - connect_time
            if duration < 5.0 and connect_time > 0:
                quick_disconnect_count += 1
                if quick_disconnect_count >= 3:
                    logger.error("Too many quick disconnects, check AppID/Secret/permissions")
                    _running = False
                    return
            else:
                quick_disconnect_count = 0

            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info(f"Reconnecting in {delay}s (attempt {backoff_idx + 1})")
            await asyncio.sleep(delay)

            try:
                if _ws_session and not _ws_session.closed:
                    await _ws_session.close()
                gateway_url = await get_gateway_url()
                import aiohttp
                _ws_session = aiohttp.ClientSession(trust_env=True)
                ws = await _ws_session.ws_connect(
                    gateway_url,
                    headers={"User-Agent": "ClaudeBridge/2.0"},
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
                logger.info("Reconnected")
            except Exception as e:
                logger.error(f"Reconnect failed: {e}")
                backoff_idx += 1
                if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                    logger.error("Max reconnect attempts reached")
                    _running = False
                    return

        except asyncio.CancelledError:
            return
        except Exception as e:
            if not _running:
                return
            logger.error(f"Event loop error: {e}")
            await asyncio.sleep(2)


# ================= 入口 =================

async def main():
    global _running, _http_client, _ws_session

    print("=" * 50)
    print("  Claude Code <-> QQ Bot (Official WebSocket)")
    print(f"  AppID: {APP_ID}")
    print(f"  Master: {MASTER_OPENID}")
    print("=" * 50)

    import httpx
    import aiohttp

    _http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    _running = True

    gateway_url = await get_gateway_url()
    logger.info(f"Gateway URL: {gateway_url}")

    _ws_session = aiohttp.ClientSession(trust_env=True)
    ws = await _ws_session.ws_connect(
        gateway_url,
        headers={"User-Agent": "ClaudeBridge/2.0"},
        timeout=aiohttp.ClientWSTimeout(ws_close=CONNECT_TIMEOUT),
    )
    logger.info("WebSocket connected")

    try:
        await event_loop(ws)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        _running = False
        try:
            await ws.close()
            await _ws_session.close()
        except Exception:
            pass
        await _http_client.aclose()
        _http_client = None
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
