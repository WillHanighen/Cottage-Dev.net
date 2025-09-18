from pathlib import Path
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from markdown_it import MarkdownIt
import bleach
from bs4 import BeautifulSoup

from app.core.db import get_session
from app.core.models.resume import Resume

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Markdown rendering and sanitization (reuse forum settings)
_md = MarkdownIt()
_ALLOWED_TAGS = [
    "p", "br", "hr", "pre", "code", "blockquote",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "em", "strong", "del", "a", "img"
]
_ALLOWED_ATTRS = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title"]
}

def _wrap_images_with_links(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for img in soup.find_all('img'): 
        if img.find_parent('a'):
            continue  # already linked by author
        src = img.get('src')
        if not src:
            continue
        a = soup.new_tag('a', href=src)
        a['target'] = '_blank'
        a['rel'] = 'noopener nofollow'
        img.replace_with(a)
        a.append(img)
    return str(soup)

def _force_links_new_tab(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for a in soup.find_all('a'):
        href = (a.get('href') or '').strip()
        if not href:
            continue
        # Open every link in a new tab and set safe rel
        a['target'] = '_blank'
        a['rel'] = 'noopener nofollow'
    return str(soup)
    
def render_markdown(text: str) -> str:
    html = _md.render(text or "")
    # Wrap images with links so images open in a new tab when clicked
    html = _wrap_images_with_links(html)
    # Force links to open in a new tab
    html = _force_links_new_tab(html)
    # Sanitize after wrapping to ensure allowed tags/attrs only
    return bleach.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True, protocols=["http", "https", "mailto"])


@router.get("/resume", response_class=HTMLResponse)
async def resume_view(request: Request, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Resume).limit(1))
    resume = result.scalar_one_or_none()
    resume_html = render_markdown(resume.content if resume else "")
    ctx = {
        "request": request,
        "title": "Resume",
        "resume": resume,
        "resume_html": resume_html,
    }
    return templates.TemplateResponse("resume.html", ctx)


@router.get("/resume/edit", response_class=HTMLResponse)
async def resume_edit_page(request: Request, session: AsyncSession = Depends(get_session)):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login?next=/resume/edit", status_code=302)
    if getattr(user, "role", "user") != "owner":
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "title": "403 Error", "code": 403, "message": "Only the site owner can edit the resume."},
            status_code=403,
        )
    result = await session.execute(select(Resume).limit(1))
    resume = result.scalar_one_or_none()
    return templates.TemplateResponse("resume_edit.html", {"request": request, "title": "Edit Resume", "resume": resume})


@router.post("/resume/edit")
async def resume_edit_submit(request: Request, content: str = Form(""), session: AsyncSession = Depends(get_session)):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login?next=/resume/edit", status_code=302)
    if getattr(user, "role", "user") != "owner":
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "title": "403 Error", "code": 403, "message": "Only the site owner can edit the resume."},
            status_code=403,
        )
    content = (content or "").strip()
    result = await session.execute(select(Resume).limit(1))
    resume = result.scalar_one_or_none()
    if not resume:
        resume = Resume(content=content, updated_by=user.id)
        session.add(resume)
    else:
        resume.content = content
        resume.updated_by = user.id
    await session.commit()
    return RedirectResponse(url="/resume", status_code=302)
