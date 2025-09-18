from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.exceptions import RequestValidationError
from fastapi.templating import Jinja2Templates
import traceback
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.redis_client import get_redis, close_redis
from app.web.routers.home import router as home_router
from app.web.routers.chat import router as chat_router
from app.web.routers.ws import router as ws_router
from app.web.routers.forum import router as forum_router
from app.web.routers.about import router as about_router
from app.web.routers.resume import router as resume_router
from app.web.routers.auth import router as auth_router
from app.middleware.auth import AuthMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.db import Base, AsyncSessionLocal
from sqlalchemy import select, text

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG)

# Auth middleware
app.add_middleware(AuthMiddleware)
# Session middleware (required for OAuth state)
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, same_site="lax", https_only=not settings.DEBUG)

# Static files
static_dir = BASE_DIR / "web" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Error templates
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


@app.on_event("startup")
async def on_startup():
  # Initialize Redis connection
  await get_redis()
  # Create tables if not exist
  # Import models to register mappers
  import app.core.models.user  # noqa: F401
  import app.core.models.thread  # noqa: F401
  import app.core.models.category  # noqa: F401
  import app.core.models.interaction  # noqa: F401
  import app.core.models.resume  # noqa: F401
  engine: AsyncEngine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG, future=True)
  async with engine.begin() as conn:
      await conn.run_sync(Base.metadata.create_all)
      # Ensure 'role' column exists on users (best-effort, Postgres-specific)
      try:
          await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT 'user'"))
      except Exception:
          pass
  # Seed default categories if none exist
  try:
      from app.core.models.category import Category
      from app.core.models.resume import Resume
      from app.core.models.user import User
      async with AsyncSessionLocal() as session:
          result = await session.execute(select(Category).limit(1))
          exists = result.scalar_one_or_none()
          if not exists:
              session.add_all([
                  Category(name="General", slug="general"),
                  Category(name="Announcements", slug="announcements"),
                  Category(name="Show & Tell", slug="show-and-tell"),
              ])
              await session.commit()
          # Seed default resume if not present
          r = (await session.execute(select(Resume).limit(1))).scalar_one_or_none()
          if not r:
              session.add(Resume(content="# Your Name\n\nAdd your resume content here (Markdown supported)."))
              await session.commit()
          # Assign owner role based on OWNER_EMAIL env, if set
          if settings.OWNER_EMAIL:
              owner_email = settings.OWNER_EMAIL.strip().lower()
              u = (await session.execute(select(User).where(User.email == owner_email))).scalar_one_or_none()
              if u and getattr(u, "role", "user") != "owner":
                  u.role = "owner"
                  await session.commit()
  except Exception:
      # Swallow seeding errors in startup path to avoid blocking app
      pass


@app.on_event("shutdown")
async def on_shutdown():
  await close_redis()


# Routers
app.include_router(home_router)
app.include_router(chat_router)
app.include_router(ws_router)
app.include_router(forum_router)
app.include_router(about_router)
app.include_router(resume_router)
app.include_router(auth_router)


@app.get("/healthz")
async def healthz():
  return {"status": "ok"}


# -------------------
# Exception Handlers
# -------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    code = exc.status_code
    default_messages = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Page not found",
        405: "Method Not Allowed",
        408: "Request Timeout",
        409: "Conflict",
        413: "Payload Too Large",
        415: "Unsupported Media Type",
        429: "Too Many Requests",
    }
    message = (exc.detail or default_messages.get(code) or "Unexpected error") if hasattr(exc, "detail") else default_messages.get(code, "Unexpected error")
    ctx = {
        "request": request,
        "title": f"{code} Error",
        "code": code,
        "message": message,
        "debug": settings.DEBUG,
        "traceback": None,
    }
    return templates.TemplateResponse("error.html", ctx, status_code=code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    code = 422
    message = "Validation Error"
    # When debugging, include the validation errors in the page for convenience
    tb = None
    if settings.DEBUG:
        tb = traceback.format_exc()
    ctx = {
        "request": request,
        "title": f"{code} {message}",
        "code": code,
        "message": message,
        "debug": settings.DEBUG,
        "traceback": tb,
        "errors": exc.errors(),
    }
    return templates.TemplateResponse("error.html", ctx, status_code=code)


@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
    # Handle Starlette-level HTTP errors (including 404 for missing routes/static)
    code = exc.status_code
    default_messages = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Page not found",
        405: "Method Not Allowed",
        408: "Request Timeout",
        409: "Conflict",
        413: "Payload Too Large",
        415: "Unsupported Media Type",
        429: "Too Many Requests",
    }
    message = (getattr(exc, "detail", None) or default_messages.get(code) or "Unexpected error")
    ctx = {
        "request": request,
        "title": f"{code} Error",
        "code": code,
        "message": message,
        "debug": settings.DEBUG,
        "traceback": None,
    }
    return templates.TemplateResponse("error.html", ctx, status_code=code)


# Only install a global 500 handler when NOT in debug mode.
if not settings.DEBUG:
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        code = 500
        message = "An internal server error occurred."
        tb = "".join(traceback.format_exception(None, exc, exc.__traceback__))
        ctx = {
            "request": request,
            "title": f"{code} Error",
            "code": code,
            "message": message,
            "debug": settings.DEBUG,
            "traceback": tb if settings.DEBUG else None,
        }
        return templates.TemplateResponse("error.html", ctx, status_code=code)


# Fun 418 endpoint
@app.get("/teapot")
async def teapot(request: Request):
    ctx = {
        "request": request,
        "title": "I'm a teapot",
    }
    return templates.TemplateResponse("teapot.html", ctx, status_code=418)
