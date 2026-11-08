"""Sign-in: an API token for scripts, and browser sessions made from one-time codes.

- The API token (32 random bytes) is made on first start and kept in the OS keychain, or in
  `<data_dir>/api_token` where there's no keychain (servers, tests). `nvx token` prints it.
  Scripts send it as `Authorization: Bearer <token>`.
- A browser signs in once, with the token or a one-time code, and gets a session cookie.
  Only a hash of the cookie is stored, so a copy of the database can't sign anyone in.
- One-time codes (`nvx signin`, or Settings → "Sign in another device") work once, for
  10 minutes. After 10 wrong tries in 10 minutes, sign-in pauses for everyone.

There's no "local requests are trusted" shortcut: Tailscale Serve forwards your phone's
requests from this computer's own address, so they would look local too.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neurovex.core.config import Profile, Settings, set_secret
from neurovex.core.db import utcnow
from neurovex.core.logging import get_logger
from neurovex.security.models import AuthCode, AuthSession

log = get_logger(__name__)

TOKEN_NAME = "NEUROVEX_API_TOKEN"
COOKIE = "nvx_session"
CODE_TTL = timedelta(minutes=10)
SESSION_IDLE = timedelta(days=90)  # a device unused this long must sign in again
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
CODE_LEN = 8
MAX_FAILURES = 10
FAILURE_WINDOW_S = 600.0
CACHE_S = 60.0  # how long a checked session or token is trusted without asking again
TOUCH_EVERY = timedelta(hours=1)


class TooManyAttempts(Exception):
    pass


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_code(raw: str) -> str | None:
    """'k7qm-4xpa' → 'K7QM4XPA'; None if it can't be a code (then it may be a token)."""
    code = re.sub(r"[\s-]", "", raw or "").upper()
    if len(code) == CODE_LEN and all(c in CODE_ALPHABET for c in code):
        return code
    return None


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _token_file(settings: Settings) -> Path:
    return settings.data_dir / "api_token"


def _uses_keychain(settings: Settings) -> bool:
    return settings.use_keyring and settings.profile is not Profile.TEST


def _write_token_file(settings: Settings, token: str) -> None:
    settings.ensure_dirs()
    path = _token_file(settings)
    path.write_text(token, encoding="utf-8")
    with contextlib.suppress(OSError):  # owner only (no effect on Windows: the folder is yours)
        os.chmod(path, 0o600)


def store_api_token(settings: Settings, token: str) -> str:
    """Save the token where `load_api_token` looks. Returns where it went."""
    if _uses_keychain(settings):
        try:
            set_secret(TOKEN_NAME, token)
            _token_file(settings).unlink(missing_ok=True)
            return "keychain"
        except Exception:  # no usable keychain (e.g. headless Linux): fall back to a file
            pass
    _write_token_file(settings, token)
    return str(_token_file(settings))


def load_api_token(settings: Settings, create: bool = True) -> str | None:
    """The API token: environment / .env / keychain, then the token file; made if missing."""
    if token := settings.secret(TOKEN_NAME):
        return token
    path = _token_file(settings)
    if path.is_file() and (token := path.read_text(encoding="utf-8").strip()):
        return token
    if not create:
        return None
    token = secrets.token_urlsafe(32)
    where = store_api_token(settings, token)
    log.info("api_token_created", stored_in="keychain" if where == "keychain" else "file")
    return token


def device_label(user_agent: str) -> str:
    """'Mozilla/5.0 (Linux; Android 15…) … Chrome/…' → 'Chrome on Android'."""
    ua = user_agent or ""
    browser = next(
        (name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                ("Chrome/", "Chrome"), ("Safari/", "Safari")) if key in ua),
        "Browser",
    )  # fmt: skip
    system = next(
        (name for key, name in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                ("Windows", "Windows"), ("Mac OS", "Mac"), ("Linux", "Linux"))
         if key in ua),
        None,
    )  # fmt: skip
    return f"{browser} on {system}" if system else browser


@dataclass
class NewCode:
    code: str  # formatted, e.g. K7QM-4XPA
    expires_at: datetime


class AuthService:
    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.settings = settings
        self.sessions = sessions
        self._token: str | None = None
        self._token_at = 0.0
        self._valid: dict[str, float] = {}  # session hash → when it was last confirmed
        self._failures: deque[float] = deque()

    # --- the API token -------------------------------------------------------------------

    def token(self) -> str:
        """Re-read now and then, so `nvx token --new` takes effect without a restart."""
        if self._token is None or time.monotonic() - self._token_at > CACHE_S:
            self._token = load_api_token(self.settings)
            self._token_at = time.monotonic()
        assert self._token
        return self._token

    def check_token(self, presented: str) -> bool:
        return bool(presented) and hmac.compare_digest(presented.encode(), self.token().encode())

    # --- failed attempts ----------------------------------------------------------------

    def _guard_attempts(self) -> None:
        now = time.monotonic()
        while self._failures and now - self._failures[0] > FAILURE_WINDOW_S:
            self._failures.popleft()
        if len(self._failures) >= MAX_FAILURES:
            raise TooManyAttempts

    def _failed(self) -> None:
        self._failures.append(time.monotonic())

    # --- one-time codes -----------------------------------------------------------------

    async def create_code(self) -> NewCode:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        expires = utcnow() + CODE_TTL
        async with self.sessions() as s:
            await s.execute(delete(AuthCode).where(AuthCode.expires_at < utcnow() - CODE_TTL))
            s.add(AuthCode(code_hash=_sha(code), expires_at=expires))
            await s.commit()
        return NewCode(format_code(code), expires)

    async def _redeem_code(self, code: str) -> bool:
        async with self.sessions() as s:
            row = await s.scalar(select(AuthCode).where(AuthCode.code_hash == _sha(code)))
            if row is None or row.used_at is not None or row.expires_at < utcnow():
                return False
            row.used_at = utcnow()
            await s.commit()
            return True

    # --- browser sessions ---------------------------------------------------------------

    async def sign_in(self, secret: str, label: str) -> tuple[str, str] | None:
        """A code or the token → (new session cookie, how). None if it's wrong."""
        self._guard_attempts()
        code = normalize_code(secret)
        if code is not None:
            ok, how = await self._redeem_code(code), "code"
        else:
            ok, how = self.check_token(secret.strip()), "token"
        if not ok:
            self._failed()
            return None
        cookie = secrets.token_urlsafe(32)
        async with self.sessions() as s:
            s.add(AuthSession(token_hash=_sha(cookie), label=label[:80] or "Browser"))
            await s.commit()
        self._valid[_sha(cookie)] = time.monotonic()
        return cookie, how

    async def session_id(self, cookie: str) -> int | None:
        """The session this cookie belongs to, if it's still signed in."""
        if not cookie:
            return None
        digest = _sha(cookie)
        async with self.sessions() as s:
            row = await s.scalar(select(AuthSession).where(AuthSession.token_hash == digest))
            if row is None or utcnow() - row.last_seen_at > SESSION_IDLE:
                self._valid.pop(digest, None)
                return None
            if utcnow() - row.last_seen_at > TOUCH_EVERY:
                row.last_seen_at = utcnow()
                await s.commit()
            return row.id

    async def check_session(self, cookie: str) -> bool:
        if not cookie:
            return False
        digest = _sha(cookie)
        seen = self._valid.get(digest)
        if seen is not None and time.monotonic() - seen < CACHE_S:
            return True
        if await self.session_id(cookie) is None:
            return False
        self._valid[digest] = time.monotonic()
        return True

    async def list_sessions(self) -> list[AuthSession]:
        async with self.sessions() as s:
            rows = await s.scalars(select(AuthSession).order_by(AuthSession.last_seen_at.desc()))
            return list(rows)

    async def sign_out(self, session_id: int) -> bool:
        async with self.sessions() as s:
            row = await s.get(AuthSession, session_id)
            if row is None:
                return False
            self._valid.pop(row.token_hash, None)
            await s.delete(row)
            await s.commit()
            return True
