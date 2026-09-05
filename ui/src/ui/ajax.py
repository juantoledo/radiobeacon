"""JSON envelope for routes that support both a plain-form POST (the
existing full-page 303-redirect PRG flow, left untouched as the no-JS
fallback) and an AJAX POST from static/ajax-forms.js, which never
navigates. A route opts in per-branch: it keeps returning
`RedirectResponse`/a full `TemplateResponse` for a plain POST, and returns
`ajax_ok`/`ajax_error` instead when `is_ajax(request)` is true.

See static/ajax-forms.js for the client half and the response contract it
expects.
"""
from fastapi.responses import JSONResponse
from starlette.requests import Request

AJAX_HEADER = "x-requested-with"
AJAX_HEADER_VALUE = "fetch"


def is_ajax(request: Request) -> bool:
    return request.headers.get(AJAX_HEADER, "").lower() == AJAX_HEADER_VALUE


def ajax_ok(
    message: str,
    *,
    fragment: str | None = None,
    target: str | None = None,
    redirect: str | None = None,
) -> JSONResponse:
    body: dict = {"ok": True, "message": message}
    if fragment is not None:
        body["fragment"] = fragment
    if target is not None:
        body["target"] = target
    if redirect is not None:
        body["redirect"] = redirect
    return JSONResponse(body)


def ajax_error(
    message: str,
    *,
    fragment: str | None = None,
    target: str | None = None,
    status_code: int = 400,
) -> JSONResponse:
    body: dict = {"ok": False, "message": message}
    if fragment is not None:
        body["fragment"] = fragment
    if target is not None:
        body["target"] = target
    return JSONResponse(body, status_code=status_code)
