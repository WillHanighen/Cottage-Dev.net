from pathlib import Path
from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, and_
from markdown_it import MarkdownIt
import bleach

from app.core.db import get_session
from app.core.models.thread import Thread, Reply
from app.core.turnstile import verify_turnstile
from app.core.models.category import Category, ThreadCategory
from app.core.models.interaction import Vote, Reaction

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Markdown rendering and sanitization
_md = MarkdownIt()
_ALLOWED_TAGS = [
    "p", "br", "hr", "pre", "code", "blockquote",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "em", "strong", "del", "a"
]
_ALLOWED_ATTRS = {"a": ["href", "title", "target", "rel"]}

def render_markdown(text: str) -> str:
    html = _md.render(text or "")
    return bleach.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)


@router.get("/forum", response_class=HTMLResponse)
async def forum_index(request: Request, cat: str | None = Query(None), session: AsyncSession = Depends(get_session)):
    # Public view; new thread requires auth
    ctx = {"request": request, "title": "Forum", "cat": cat}
    # Load categories for sidebar
    result = await session.execute(select(Category).order_by(Category.name))
    ctx["categories"] = result.scalars().all()
    return templates.TemplateResponse("forum_index.html", ctx)


@router.get("/forum/threads", response_class=HTMLResponse)
async def forum_threads(request: Request, session: AsyncSession = Depends(get_session), cat: str | None = Query(None)):
    # Load latest threads; compute simple excerpt and replies count
    stmt = select(
        Thread.id,
        Thread.title,
        Thread.body,
        func.count(Reply.id).label("replies_count"),
    ).join(Reply, Reply.thread_id == Thread.id, isouter=True)
    if cat:
        stmt = (
            stmt.join(ThreadCategory, ThreadCategory.thread_id == Thread.id, isouter=True)
                .join(Category, Category.id == ThreadCategory.category_id, isouter=True)
                .where(Category.slug == cat)
        )
    stmt = stmt.group_by(Thread.id, Thread.title, Thread.body).order_by(desc(Thread.created_at)).limit(20)
    result = await session.execute(stmt)
    rows = result.all()
    threads = [
        {
            "id": row.id,
            "title": row.title,
            "excerpt": (row.body or "")[:160] + ("…" if (row.body and len(row.body) > 160) else ""),
            "replies_count": int(row.replies_count or 0),
        }
        for row in rows
    ]
    return templates.TemplateResponse("partials/_threads.html", {"request": request, "threads": threads})


@router.get("/forum/new", response_class=HTMLResponse)
async def forum_new(request: Request, session: AsyncSession = Depends(get_session)):
    # Require auth to view form
    if not getattr(request.state, "user", None):
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=302)
    from app.core.config import settings
    cats = (await session.execute(select(Category).order_by(Category.name))).scalars().all()
    return templates.TemplateResponse(
        "forum_new.html",
        {"request": request, "title": "New Thread", "turnstile_site_key": settings.TURNSTILE_SITE_KEY, "categories": cats},
    )


@router.post("/forum/new")
async def forum_new_submit(
    request: Request,
    title: str = Form(...),
    body: str = Form(...),
    category: str | None = Form(None),  # slug
    cf_token: str = Form(alias="cf-turnstile-response"),
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url=f"/login?next=/forum/new", status_code=302)
    # Verify Turnstile
    ok = await verify_turnstile(cf_token)
    if not ok:
        from app.core.config import settings
        return templates.TemplateResponse(
            "forum_new.html",
            {"request": request, "title": "New Thread", "error": "Failed challenge. Please try again.", "turnstile_site_key": settings.TURNSTILE_SITE_KEY},
            status_code=400,
        )
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        from app.core.config import settings
        return templates.TemplateResponse(
            "forum_new.html",
            {"request": request, "title": "New Thread", "error": "Title and body are required.", "turnstile_site_key": settings.TURNSTILE_SITE_KEY},
            status_code=400,
        )
    t = Thread(title=title, body=body, user_id=user.id)
    session.add(t)
    await session.flush()
    # Attach category if provided
    if category:
        cat = (await session.execute(select(Category).where(Category.slug == category))).scalar_one_or_none()
        if cat:
            session.add(ThreadCategory(thread_id=t.id, category_id=cat.id))
    await session.commit()
    return RedirectResponse(url="/forum", status_code=302)


@router.get("/forum/thread/{thread_id}", response_class=HTMLResponse)
async def forum_thread_view(request: Request, thread_id: int, session: AsyncSession = Depends(get_session)):
    t = await session.get(Thread, thread_id)
    if not t:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "title": "404 Error", "code": 404, "message": "Thread not found"},
            status_code=404,
        )
    # Category
    cat = (await session.execute(
        select(Category).join(ThreadCategory, ThreadCategory.category_id == Category.id).where(ThreadCategory.thread_id == thread_id)
    )).scalar_one_or_none()
    # Replies
    result = await session.execute(select(Reply).where(Reply.thread_id == thread_id).order_by(Reply.created_at))
    replies = result.scalars().all()
    reply_ids = [r.id for r in replies]
    # Votes
    thread_score = (await session.execute(
        select(func.coalesce(func.sum(Vote.value), 0)).where(and_(Vote.entity_type == "thread", Vote.entity_id == thread_id))
    )).scalar_one()
    reply_scores = {}
    if reply_ids:
        rows = (await session.execute(
            select(Vote.entity_id, func.coalesce(func.sum(Vote.value), 0)).where(and_(Vote.entity_type == "reply", Vote.entity_id.in_(reply_ids))).group_by(Vote.entity_id)
        )).all()
        reply_scores = {rid: score for rid, score in rows}
    # Reactions
    thread_reactions = (await session.execute(
        select(Reaction.key, func.count(Reaction.id)).where(and_(Reaction.entity_type == "thread", Reaction.entity_id == thread_id)).group_by(Reaction.key)
    )).all()
    reply_reactions = {}
    if reply_ids:
        rows = (await session.execute(
            select(Reaction.entity_id, Reaction.key, func.count(Reaction.id)).where(and_(Reaction.entity_type == "reply", Reaction.entity_id.in_(reply_ids))).group_by(Reaction.entity_id, Reaction.key)
        )).all()
        for rid, key, cnt in rows:
            reply_reactions.setdefault(rid, {})[key] = cnt
    ctx = {
        "request": request,
        "title": t.title,
        "thread": t,
        "category": cat,
        "thread_html": render_markdown(t.body),
        "replies": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "created_at": r.created_at,
                "body_html": render_markdown(r.body),
                "score": int(reply_scores.get(r.id, 0)),
                "reactions": reply_reactions.get(r.id, {}),
            }
            for r in replies
        ],
        "thread_score": int(thread_score or 0),
        "thread_reactions": {k: v for k, v in thread_reactions},
    }
    return templates.TemplateResponse("forum_thread.html", ctx)


@router.post("/forum/thread/{thread_id}/reply")
async def forum_thread_reply(
    request: Request,
    thread_id: int,
    body: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url=f"/login?next=/forum/thread/{thread_id}", status_code=302)
    t = await session.get(Thread, thread_id)
    if not t:
        return RedirectResponse(url="/forum", status_code=302)
    body = (body or "").strip()
    if not body:
        result = await session.execute(
            select(Reply).where(Reply.thread_id == thread_id).order_by(Reply.created_at)
        )
        replies = result.scalars().all()
        ctx = {
            "request": request,
            "title": t.title,
            "thread": t,
            "thread_html": render_markdown(t.body),
            "error": "Reply cannot be empty.",
            "replies": [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    "created_at": r.created_at,
                    "body_html": render_markdown(r.body),
                }
                for r in replies
            ],
        }
        return templates.TemplateResponse("forum_thread.html", ctx, status_code=400)
    r = Reply(thread_id=thread_id, user_id=user.id, body=body)
    session.add(r)
    await session.commit()
    return RedirectResponse(url=f"/forum/thread/{thread_id}#reply-{r.id}", status_code=302)


# -----------------
# Edit Endpoints
# -----------------

@router.get("/forum/thread/{thread_id}/edit", response_class=HTMLResponse)
async def forum_thread_edit(request: Request, thread_id: int, session: AsyncSession = Depends(get_session)):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url=f"/login?next=/forum/thread/{thread_id}", status_code=302)
    t = await session.get(Thread, thread_id)
    if not t or t.user_id != user.id:
        return RedirectResponse(url=f"/forum/thread/{thread_id}", status_code=302)
    return templates.TemplateResponse("forum_thread_edit.html", {"request": request, "title": f"Edit: {t.title}", "thread": t})


@router.post("/forum/thread/{thread_id}/edit")
async def forum_thread_edit_submit(request: Request, thread_id: int, title: str = Form(...), body: str = Form(...), session: AsyncSession = Depends(get_session)):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url=f"/login?next=/forum/thread/{thread_id}", status_code=302)
    t = await session.get(Thread, thread_id)
    if not t or t.user_id != user.id:
        return RedirectResponse(url=f"/forum/thread/{thread_id}", status_code=302)
    t.title = (title or "").strip()
    t.body = (body or "").strip()
    await session.commit()
    return RedirectResponse(url=f"/forum/thread/{thread_id}", status_code=302)


@router.get("/forum/reply/{reply_id}/edit", response_class=HTMLResponse)
async def forum_reply_edit(request: Request, reply_id: int, session: AsyncSession = Depends(get_session)):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url=f"/login", status_code=302)
    r = await session.get(Reply, reply_id)
    if not r or r.user_id != user.id:
        return RedirectResponse(url=f"/forum", status_code=302)
    return templates.TemplateResponse("forum_reply_edit.html", {"request": request, "title": "Edit reply", "reply": r})


@router.post("/forum/reply/{reply_id}/edit")
async def forum_reply_edit_submit(request: Request, reply_id: int, body: str = Form(...), session: AsyncSession = Depends(get_session)):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url=f"/login", status_code=302)
    r = await session.get(Reply, reply_id)
    if not r or r.user_id != user.id:
        return RedirectResponse(url=f"/forum", status_code=302)
    r.body = (body or "").strip()
    await session.commit()
    return RedirectResponse(url=f"/forum/thread/{r.thread_id}#reply-{r.id}", status_code=302)


# -----------------
# Votes & Reactions
# -----------------

def _toggle_vote_sql(entity_type: str, entity_id: int, user_id: int, value: int):
    return entity_type, entity_id, user_id, value


@router.post("/forum/thread/{thread_id}/vote")
async def forum_thread_vote(request: Request, thread_id: int, action: str = Form(...), session: AsyncSession = Depends(get_session)):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url=f"/login?next=/forum/thread/{thread_id}", status_code=302)
    v = 1 if action == "up" else -1
    # Fetch existing
    row = (await session.execute(select(Vote).where(and_(Vote.entity_type == "thread", Vote.entity_id == thread_id, Vote.user_id == user.id)))).scalar_one_or_none()
    if row and row.value == v:
        await session.delete(row)
    elif row:
        row.value = v
    else:
        session.add(Vote(entity_type="thread", entity_id=thread_id, user_id=user.id, value=v))
    await session.commit()
    return RedirectResponse(url=f"/forum/thread/{thread_id}", status_code=302)


@router.post("/forum/reply/{reply_id}/vote")
async def forum_reply_vote(request: Request, reply_id: int, action: str = Form(...), session: AsyncSession = Depends(get_session)):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url=f"/login", status_code=302)
    v = 1 if action == "up" else -1
    row = (await session.execute(select(Vote).where(and_(Vote.entity_type == "reply", Vote.entity_id == reply_id, Vote.user_id == user.id)))).scalar_one_or_none()
    if row and row.value == v:
        await session.delete(row)
    elif row:
        row.value = v
    else:
        session.add(Vote(entity_type="reply", entity_id=reply_id, user_id=user.id, value=v))
    await session.commit()
    # Find thread id to redirect
    r = await session.get(Reply, reply_id)
    return RedirectResponse(url=f"/forum/thread/{r.thread_id}#reply-{reply_id}", status_code=302)


@router.post("/forum/{entity}/{entity_id}/react")
async def forum_react(request: Request, entity: str, entity_id: int, key: str = Form(...), session: AsyncSession = Depends(get_session)):
    if entity not in {"thread", "reply"}:
        return RedirectResponse(url="/forum", status_code=302)
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url=f"/login", status_code=302)
    row = (await session.execute(select(Reaction).where(and_(Reaction.entity_type == entity, Reaction.entity_id == entity_id, Reaction.user_id == user.id, Reaction.key == key)))).scalar_one_or_none()
    if row:
        await session.delete(row)
    else:
        session.add(Reaction(entity_type=entity, entity_id=entity_id, user_id=user.id, key=key))
    await session.commit()
    # Redirect back to thread view
    thread_id = entity_id if entity == "thread" else (await session.get(Reply, entity_id)).thread_id
    return RedirectResponse(url=f"/forum/thread/{thread_id}", status_code=302)
