"""The /about page — a plain-language description of what RadioBeacon is
and how it works, aimed at product / emergency-management readers rather
than IT. Informational only: no DB access, no forms, visible to any
logged-in user (including the read-only 'user' role)."""
from fastapi import APIRouter, Depends, Request

from ..current_user import get_current_user
from ..templating import templates

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("/about")
def about_page(request: Request):
    return templates.TemplateResponse(request, "about.html", {})
