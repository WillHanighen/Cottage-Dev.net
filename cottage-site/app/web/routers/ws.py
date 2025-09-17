import asyncio
import json
from pathlib import Path
import time
from uuid import uuid4
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.redis_client import get_redis
from app.core.db import AsyncSessionLocal
from app.core.security import get_user_from_websocket
from app.core.turnstile import verify_turnstile

router = APIRouter()

CHANNEL_NAME = "chat:global"
HISTORY_KEY = "chat:global:history"

# Avatars live under app/web/static/avatars
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
AVATAR_DIR = STATIC_DIR / "avatars"


def _avatar_url(user_id: Optional[int]) -> Optional[str]:
    try:
        if not user_id:
            return None
        p = (AVATAR_DIR / f"{user_id}.webp")
        if not p.exists():
            return None
        ts = int(p.stat().st_mtime)
        return f"/static/avatars/{user_id}.webp?v={ts}"
    except Exception:
        return None


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket):
    await websocket.accept()
    # Determine user if present (for sending). Unauthed users can still receive.
    async with AsyncSessionLocal() as db:
        user = await get_user_from_websocket(websocket, db)
    display_name: Optional[str] = (user.name or user.email)[:32] if user else None

    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL_NAME)

    # Send last 50 messages as history on connect (oldest -> newest)
    try:
        raw = await redis.lrange(HISTORY_KEY, 0, 49)
        history = []
        for item in reversed(raw):  # reverse to oldest-first for display
            if isinstance(item, bytes):
                item = item.decode("utf-8", errors="ignore")
            try:
                history.append(json.loads(item))
            except Exception:
                pass
        await websocket.send_text(json.dumps({"type": "history", "items": history}))
    except Exception:
        pass

    async def reader():
        try:
            async for message in pubsub.listen():
                if message is None:
                    continue
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="ignore")
                await websocket.send_text(data)
        except Exception:
            # Connection closed or pubsub error
            pass

    async def writer():
        try:
            while True:
                text = await websocket.receive_text()
                try:
                    payload = json.loads(text)
                    # Ignore client-provided username; use authenticated user's display name
                    msg_text = str(payload.get("text") or "").strip()
                    ts = int(payload.get("ts") or 0) or int(__import__("time").time() * 1000)
                    client_id = payload.get("id")
                except Exception:
                    msg_text = text
                    ts = int(__import__("time").time() * 1000)
                    client_id = None
                if not msg_text:
                    continue
                # Only authenticated users may send
                if not display_name:
                    continue
                # Validate message length (max 2000 chars)
                if len(msg_text) > 2000:
                    try:
                        await websocket.send_text(json.dumps({"type": "error", "error": "Message exceeds 2000 characters.", "code": "too_long", "client_id": client_id}))
                    except Exception:
                        pass
                    continue
                # --- Rate limiting & anti-abuse ---
                try:
                    sender_key = f"user:{getattr(user, 'id', None)}" if user else f"ip:{getattr(websocket.client, 'host', 'unknown')}"
                    block_key = f"chat:block:{sender_key}"
                    count_key = f"chat:count:{sender_key}"
                    strikes_key = f"chat:strikes:{sender_key}"
                    challenge_key = f"chat:challenge:{sender_key}"

                    # If currently blocked, return error with retry time
                    ttl = await redis.ttl(block_key)
                    if ttl and ttl > 0:
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "error",
                                "error": f"You're sending messages too fast. Temporarily blocked. Try again in {ttl} seconds.",
                                "code": "blocked",
                                "retry_after": ttl,
                                "client_id": client_id,
                            }))
                        except Exception:
                            pass
                        continue

                    # Simple burst counter: allow up to 5 messages per 10s
                    cnt = await redis.incr(count_key)
                    if cnt == 1:
                        await redis.expire(count_key, 10)

                    require_challenge = await redis.exists(challenge_key)
                    if cnt > 5:
                        strikes = await redis.incr(strikes_key)
                        if strikes == 1:
                            await redis.expire(strikes_key, 120)
                        if strikes >= 3:
                            await redis.set(block_key, 1, ex=60)
                            try:
                                await websocket.send_text(json.dumps({
                                    "type": "error",
                                    "error": "Temporarily blocked for excessive messaging. Please wait a minute.",
                                    "code": "blocked",
                                    "retry_after": 60,
                                    "client_id": client_id,
                                }))
                            except Exception:
                                pass
                            continue
                        else:
                            await redis.set(challenge_key, 1, ex=120)
                            require_challenge = 1

                    # If a Turnstile token is provided, verify it (when challenge required)
                    cf_token = payload.get("cf")
                    if require_challenge:
                        ok = False
                        if cf_token:
                            ok = await verify_turnstile(cf_token, getattr(websocket.client, "host", None) if hasattr(websocket, "client") else None)
                        if not ok:
                            try:
                                await websocket.send_text(json.dumps({
                                    "type": "error",
                                    "error": "Additional verification required. Please complete the challenge.",
                                    "code": "challenge_required",
                                    "client_id": client_id,
                                }))
                            except Exception:
                                pass
                            continue
                        else:
                            try:
                                await redis.delete(challenge_key)
                            except Exception:
                                pass
                except Exception:
                    # If Redis unavailable, fail-open (no throttle)
                    pass
                # Attach avatar URL if available
                avatar = _avatar_url(getattr(user, "id", None))
                event = {"id": str(uuid4()), "user": display_name, "text": msg_text, "ts": ts, "avatar": avatar, "client_id": client_id}
                encoded = json.dumps(event)
                await redis.publish(CHANNEL_NAME, encoded)
                # Persist to history (LPUSH newest first), keep only last 50
                try:
                    await redis.lpush(HISTORY_KEY, encoded)
                    await redis.ltrim(HISTORY_KEY, 0, 49)
                except Exception:
                    pass
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    reader_task = asyncio.create_task(reader())
    writer_task = asyncio.create_task(writer())

    try:
        await asyncio.gather(reader_task, writer_task)
    except Exception:
        pass
    finally:
        for task in (reader_task, writer_task):
            if not task.done():
                task.cancel()
        try:
            await pubsub.unsubscribe(CHANNEL_NAME)
            await pubsub.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
