from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from pathlib import Path
import time

from app.core.db import AsyncSessionLocal
from app.core.security import get_user_from_cookie


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Default no user
        request.state.user = None
        request.state.avatar_url = None
        # Create a short-lived DB session to resolve user from cookie
        async with AsyncSessionLocal() as session:
            user = await get_user_from_cookie(request, session)
            request.state.user = user
            # Compute avatar URL if available
            try:
                if user and getattr(user, "id", None):
                    static_dir = Path(__file__).resolve().parent.parent / "web" / "static"
                    avatar_path = static_dir / "avatars" / f"{user.id}.webp"
                    if avatar_path.exists():
                        ts = int(avatar_path.stat().st_mtime)
                        request.state.avatar_url = f"/static/avatars/{user.id}.webp?v={ts}"
            except Exception:
                request.state.avatar_url = None
        response = await call_next(request)
        return response
