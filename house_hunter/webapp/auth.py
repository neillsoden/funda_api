"""Session-based login for the household accounts, with brute-force rate
limiting. This app is reachable from the public internet (home.amglab.dev),
so this is treated as a real login, not a formality.
"""

import os
import time
from functools import wraps
from urllib.parse import urlparse

from flask import redirect, request, session, url_for
from werkzeug.security import check_password_hash

_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 15 * 60
_failed_attempts: dict[str, list[float]] = {}


def _load_users() -> dict[str, str]:
    """{username: password_hash}, parsed from AUTH_USERS env var:
    "neill:<hash>,yvonne:<hash>"
    """
    raw = os.environ.get("AUTH_USERS", "")
    users: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        username, password_hash = entry.split(":", 1)
        users[username.strip().lower()] = password_hash.strip()
    return users


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def is_rate_limited() -> bool:
    ip = _client_ip()
    now = time.time()
    attempts = [t for t in _failed_attempts.get(ip, []) if now - t < _WINDOW_SECONDS]
    _failed_attempts[ip] = attempts
    return len(attempts) >= _MAX_ATTEMPTS


def record_failed_attempt() -> None:
    ip = _client_ip()
    _failed_attempts.setdefault(ip, []).append(time.time())


def verify_credentials(username: str, password: str) -> bool:
    users = _load_users()
    password_hash = users.get(username.strip().lower())
    if not password_hash:
        return False
    return check_password_hash(password_hash, password)


def safe_next_url(candidate: str | None) -> str:
    """Only allow redirecting to a same-site relative path after login -
    blocks it being used as an open redirect (e.g. next=https://evil.example.com)."""
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return url_for("form")
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return url_for("form")
    return candidate


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("user"):
            return view(*args, **kwargs)
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

    return wrapped
