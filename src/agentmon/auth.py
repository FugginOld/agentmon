"""The web server's access token: one random secret in ``~/.agentmon/web-token`` (owner-only).

Stdlib only, so scripts and emitters can read the same file the server uses.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from . import paths

MIN_LENGTH = 32


def token_path() -> Path:
    return paths.home() / "web-token"


def load_or_create() -> str:
    """The stored token, created on first use. A missing, short or unreadable file is replaced."""
    path = token_path()
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= MIN_LENGTH:
            return token
    except OSError:
        pass
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secrets.token_urlsafe(32) + "\n")
    os.replace(tmp, path)
    return path.read_text(encoding="utf-8").strip()  # two servers starting together end up sharing the winner's
