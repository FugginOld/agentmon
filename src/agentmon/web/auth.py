"""Token authentication for the web server.

Every request must present the token as ``Authorization: Bearer <token>`` (curl, emitters) or as the
``agentmon_token`` cookie (the browser). Opening ``/?token=<token>`` sets that cookie and redirects to a URL
without the token. Plain ASGI rather than ``BaseHTTPMiddleware`` so the SSE stream is not buffered.
"""

from __future__ import annotations

import hmac
from urllib.parse import parse_qsl, urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE = "agentmon_token"
CHALLENGE = {"WWW-Authenticate": "Bearer"}


def _same(candidate: str | None, token: str) -> bool:
    return candidate is not None and hmac.compare_digest(candidate.encode("utf-8"), token.encode("utf-8"))


class TokenAuth:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.token = app, token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        header = request.headers.get("authorization", "")
        bearer = header[7:].strip() if header[:7].lower() == "bearer " else None
        if _same(bearer, self.token) or _same(request.cookies.get(COOKIE), self.token):
            await self.app(scope, receive, send)
            return
        if request.method == "GET" and _same(request.query_params.get("token"), self.token):
            rest = [(k, v) for k, v in parse_qsl(request.url.query, keep_blank_values=True) if k != "token"]
            target = "/" + request.url.path.lstrip("/")  # one leading slash: never a protocol-relative URL
            response = RedirectResponse(target + ("?" + urlencode(rest) if rest else ""), status_code=303)
            response.set_cookie(COOKIE, self.token, httponly=True, samesite="strict", path="/")
        elif request.url.path.startswith("/api/"):
            response = JSONResponse({"error": "authentication required"}, status_code=401, headers=CHALLENGE)
        else:
            response = PlainTextResponse(
                "agentmon: authentication required.\nOpen the URL printed by `agentmon web` (it ends in ?token=...).\n",
                status_code=401, headers=CHALLENGE)
        await response(scope, receive, send)
