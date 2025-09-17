from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.core.config import settings

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    me_name = None
    me_avatar = None
    try:
        if getattr(request.state, "user", None):
            me_name = (request.state.user.name or request.state.user.email)
            me_avatar = getattr(request.state, "avatar_url", None)
    except Exception:
        pass
    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "title": "Chat",
            "turnstile_site_key": settings.TURNSTILE_SITE_KEY,
            "me_name": me_name,
            "me_avatar_url": me_avatar,
        },
    )
