from typing import Optional

import httpx

from app.core.config import settings


async def verify_turnstile(token: Optional[str], remoteip: Optional[str] = None) -> bool:
    # If not configured, treat as disabled and allow
    if not settings.TURNSTILE_SECRET_KEY:
        return True
    if not token:
        return False
    data = {
        "secret": settings.TURNSTILE_SECRET_KEY,
        "response": token,
    }
    if remoteip:
        data["remoteip"] = remoteip
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data)
            r.raise_for_status()
            js = r.json()
            return bool(js.get("success"))
    except Exception:
        return False
