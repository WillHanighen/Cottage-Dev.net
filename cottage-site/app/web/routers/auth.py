from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from authlib.integrations.starlette_client import OAuth
from PIL import Image
import io
import time

from app.core.config import settings
from app.core.db import get_session
from app.core.models.user import User
from app.core.security import (
    create_access_token,
    get_password_hash,
    verify_password,
)
from app.core.turnstile import verify_turnstile

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Avatars live under app/web/static/avatars
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
AVATAR_DIR = STATIC_DIR / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)

# OAuth client (registered only when credentials exist)
oauth = OAuth()
if settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
if settings.GITHUB_CLIENT_ID and settings.GITHUB_CLIENT_SECRET:
    oauth.register(
        name="github",
        client_id=settings.GITHUB_CLIENT_ID,
        client_secret=settings.GITHUB_CLIENT_SECRET,
        access_token_url="https://github.com/login/oauth/access_token",
        authorize_url="https://github.com/login/oauth/authorize",
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": "read:user user:email"},
    )

def _find_avatar_file(user_id: int) -> Optional[Path]:
    for ext in ("webp", "png", "jpg", "jpeg"):
        p = AVATAR_DIR / f"{user_id}.{ext}"
        if p.exists():
            return p
    return None


def _avatar_url(user_id: int) -> Optional[str]:
    p = _find_avatar_file(user_id)
    if not p:
        return None
    try:
        ts = int(p.stat().st_mtime)
    except Exception:
        ts = int(time.time())
    return f"/static/avatars/{p.name}?v={ts}"


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "title": "Login", "turnstile_site_key": settings.TURNSTILE_SITE_KEY})


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    cf_token: Optional[str] = Form(default=None, alias="cf-turnstile-response"),
    next: Optional[str] = Form(default="/"),
    session: AsyncSession = Depends(get_session),
):
    # Verify Turnstile first
    if not await verify_turnstile(cf_token):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "title": "Login", "error": "Failed challenge. Please try again.", "turnstile_site_key": settings.TURNSTILE_SITE_KEY},
            status_code=400,
        )
    result = await session.execute(select(User).where(User.email == email.lower()))
    user = result.scalar_one_or_none()
    if not user or not user.password_hash or not verify_password(password, user.password_hash):
        # Re-render with error
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "title": "Login", "error": "Invalid email or password.", "turnstile_site_key": settings.TURNSTILE_SITE_KEY},
            status_code=400,
        )
    token = create_access_token(str(user.id))
    resp = RedirectResponse(url=next or "/", status_code=302)
    resp.set_cookie(
        settings.JWT_COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.JWT_COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )
    return resp


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "title": "Register", "turnstile_site_key": settings.TURNSTILE_SITE_KEY})


@router.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(...),
    name: Optional[str] = Form(default=None),
    password: str = Form(...),
    cf_token: Optional[str] = Form(default=None, alias="cf-turnstile-response"),
    next: Optional[str] = Form(default="/"),
    session: AsyncSession = Depends(get_session),
):
    if not await verify_turnstile(cf_token):
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "title": "Register", "error": "Failed challenge. Please try again.", "turnstile_site_key": settings.TURNSTILE_SITE_KEY},
            status_code=400,
        )
    email = email.strip().lower()
    if not email or not password:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "title": "Register", "error": "Email and password are required.", "turnstile_site_key": settings.TURNSTILE_SITE_KEY},
            status_code=400,
        )
    # Check existing
    result = await session.execute(select(User).where(User.email == email))
    existing = result.scalar_one_or_none()
    if existing:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "title": "Register", "error": "Email is already registered.", "turnstile_site_key": settings.TURNSTILE_SITE_KEY},
            status_code=400,
        )
    user = User(email=email, name=name or None, password_hash=get_password_hash(password), provider="local")
    session.add(user)
    await session.commit()
    await session.refresh(user)

    token = create_access_token(str(user.id))
    resp = RedirectResponse(url=next or "/", status_code=302)
    resp.set_cookie(
        settings.JWT_COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.JWT_COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie(settings.JWT_COOKIE_NAME, path="/")
    return resp


@router.get("/auth/google/login")
async def google_login(request: Request):
    if not getattr(oauth, "google", None):
        return RedirectResponse(url="/login")
    redirect_uri = settings.GOOGLE_REDIRECT_URL or str(request.url_for("google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/google/callback")
async def google_callback(request: Request, session: AsyncSession = Depends(get_session)):
    if not getattr(oauth, "google", None):
        return RedirectResponse(url="/login")
    token = await oauth.google.authorize_access_token(request)
    # Prefer OpenID profile
    userinfo = token.get("userinfo")
    if not userinfo:
        resp = await oauth.google.get("userinfo", token=token)
        userinfo = resp.json()
    sub = str(userinfo.get("sub"))
    email = (userinfo.get("email") or "").lower()
    name = userinfo.get("name")

    # Find by provider_sub or email fallback
    result = await session.execute(select(User).where((User.provider == "google") & (User.provider_sub == sub)))
    user = result.scalar_one_or_none()
    if not user and email:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
    if not user:
        user = User(email=email or f"google_{sub}@example.com", name=name, provider="google", provider_sub=sub)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        # Ensure provider fields are stored
        if not user.provider_sub:
            user.provider = "google"
            user.provider_sub = sub
            await session.commit()

    jwt_token = create_access_token(str(user.id))
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(settings.JWT_COOKIE_NAME, jwt_token, httponly=True, secure=settings.JWT_COOKIE_SECURE, samesite="lax", max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60, path="/")
    return resp


@router.get("/auth/github/login")
async def github_login(request: Request):
    if not getattr(oauth, "github", None):
        return RedirectResponse(url="/login")
    redirect_uri = settings.GITHUB_REDIRECT_URL or str(request.url_for("github_callback"))
    return await oauth.github.authorize_redirect(request, redirect_uri)


@router.get("/auth/github/callback")
async def github_callback(request: Request, session: AsyncSession = Depends(get_session)):
    if not getattr(oauth, "github", None):
        return RedirectResponse(url="/login")
    token = await oauth.github.authorize_access_token(request)
    resp = await oauth.github.get("user", token=token)
    data = resp.json()
    gid = str(data.get("id"))
    name = data.get("name") or data.get("login")
    email = data.get("email") or ""
    # Sometimes GitHub email is private; fetch primary emails
    if not email:
        emails_resp = await oauth.github.get("user/emails", token=token)
        emails = emails_resp.json() if emails_resp.status_code == 200 else []
        primary = next((e for e in emails if e.get("primary")), None)
        email = (primary or {}).get("email") or ""
    email = email.lower() if email else None

    result = await session.execute(select(User).where((User.provider == "github") & (User.provider_sub == gid)))
    user = result.scalar_one_or_none()
    if not user and email:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
    if not user:
        user = User(email=email or f"github_{gid}@example.com", name=name, provider="github", provider_sub=gid)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        if not user.provider_sub:
            user.provider = "github"
            user.provider_sub = gid
            await session.commit()

    jwt_token = create_access_token(str(user.id))
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(settings.JWT_COOKIE_NAME, jwt_token, httponly=True, secure=settings.JWT_COOKIE_SECURE, samesite="lax", max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60, path="/")
    return resp


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, session: AsyncSession = Depends(get_session)):
    # Require login; redirect to login with next param if anonymous
    if not getattr(request.state, "user", None):
        next_url = "/account"
        return RedirectResponse(url=f"/login?next={next_url}")
    # Load fresh user from DB for display
    result = await session.execute(select(User).where(User.id == request.state.user.id))
    user = result.scalar_one_or_none()
    return templates.TemplateResponse(
        "account.html",
        {"request": request, "title": "Account settings", "user": user, "avatar_url": _avatar_url(user.id) if user else None},
    )


@router.post("/account/profile", response_class=HTMLResponse)
async def account_update_profile(
    request: Request,
    name: Optional[str] = Form(default=None),
    email: Optional[str] = Form(default=None),
    session: AsyncSession = Depends(get_session),
):
    if not getattr(request.state, "user", None):
        return RedirectResponse(url="/login?next=/account", status_code=302)

    name = (name or "").strip() or None
    email = (email or "").strip().lower() or None

    # Load current user
    result = await session.execute(select(User).where(User.id == request.state.user.id))
    user = result.scalar_one_or_none()
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # Validate email uniqueness if changed
    if email and email != (user.email or "").lower():
        check = await session.execute(select(User).where(User.email == email))
        existing = check.scalar_one_or_none()
        if existing and existing.id != user.id:
            return templates.TemplateResponse(
                "account.html",
                {
                    "request": request,
                    "title": "Account settings",
                    "user": user,
                    "profile_error": "Email is already in use.",
                },
                status_code=400,
            )

    # Apply updates
    if name is not None:
        user.name = name
    if email is not None:
        user.email = email
    await session.commit()
    await session.refresh(user)

    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "title": "Account settings",
            "user": user,
            "profile_success": "Profile updated.",
            "avatar_url": _avatar_url(user.id),
        },
    )


@router.post("/account/password", response_class=HTMLResponse)
async def account_change_password(
    request: Request,
    current_password: Optional[str] = Form(default=None),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    if not getattr(request.state, "user", None):
        return RedirectResponse(url="/login?next=/account", status_code=302)

    # Load current user
    result = await session.execute(select(User).where(User.id == request.state.user.id))
    user = result.scalar_one_or_none()
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # Only allow password changes for non-OAuth users
    if (user.provider or "local") != "local":
        return templates.TemplateResponse(
            "account.html",
            {
                "request": request,
                "title": "Account settings",
                "user": user,
                "password_error": "Password changes are only available for local accounts (non-OAuth).",
            },
            status_code=400,
        )

    # Validate new password
    if new_password != confirm_password:
        return templates.TemplateResponse(
            "account.html",
            {
                "request": request,
                "title": "Account settings",
                "user": user,
                "password_error": "New passwords do not match.",
            },
            status_code=400,
        )
    if len(new_password) < 8:
        return templates.TemplateResponse(
            "account.html",
            {
                "request": request,
                "title": "Account settings",
                "user": user,
                "password_error": "Password must be at least 8 characters.",
            },
            status_code=400,
        )

    # If user already has a password, verify current_password
    if user.password_hash:
        if not current_password or not verify_password(current_password, user.password_hash):
            return templates.TemplateResponse(
                "account.html",
                {
                    "request": request,
                    "title": "Account settings",
                    "user": user,
                    "password_error": "Current password is incorrect.",
                },
                status_code=400,
            )

    # Set new password
    user.password_hash = get_password_hash(new_password)
    await session.commit()
    await session.refresh(user)

    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "title": "Account settings",
            "user": user,
            "password_success": "Password updated.",
            "avatar_url": _avatar_url(user.id),
        },
    )


@router.post("/account/avatar", response_class=HTMLResponse)
async def account_update_avatar(
    request: Request,
    avatar: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    if not getattr(request.state, "user", None):
        return RedirectResponse(url="/login?next=/account", status_code=302)

    result = await session.execute(select(User).where(User.id == request.state.user.id))
    user = result.scalar_one_or_none()
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    error = None
    try:
        content_type = (avatar.content_type or "").lower()
        if not content_type.startswith("image/"):
            error = "Please upload an image file."
            raise ValueError("not image")
        raw = await avatar.read()
        if len(raw) > 5 * 1024 * 1024:
            error = "Image must be 5MB or smaller."
            raise ValueError("too large")
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        # Center-crop square
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        # Resize to 512x512 and save as WEBP
        img = img.resize((512, 512))
        out_path = AVATAR_DIR / f"{user.id}.webp"
        img.save(out_path, format="WEBP", quality=90, method=6)
    except Exception:
        if error is None:
            error = "Failed to process image. Please try a different file."

    ctx = {
        "request": request,
        "title": "Account settings",
        "user": user,
        "avatar_url": _avatar_url(user.id),
    }
    if error:
        ctx["avatar_error"] = error
        return templates.TemplateResponse("account.html", ctx, status_code=400)
    else:
        ctx["avatar_success"] = "Profile picture updated."
        return templates.TemplateResponse("account.html", ctx)
