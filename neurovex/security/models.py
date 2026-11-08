"""Tables for sign-in (browser sessions, one-time codes) and the audit log."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from neurovex.core.db import Base, UTCDateTime, utcnow


class AuthSession(Base):
    """A signed-in browser. Only a hash of its cookie is stored."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(80), default="Browser")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AuthCode(Base):
    """A one-time sign-in code (for a phone, or the link `nvx signin` prints)."""

    __tablename__ = "auth_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditEntry(Base):
    """One link in the audit chain: each entry's hash covers the previous one's."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(40))  # chat | user | api
    action: Mapped[str] = mapped_column(String(80))  # a skill id, or auth.login …
    params_hash: Mapped[str] = mapped_column(String(64))  # what was asked, without storing it
    outcome: Mapped[str] = mapped_column(String(40))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class PermissionGrant(Base):
    """Whether chat may use one permission (e.g. `tasks.write`), and who decided."""

    __tablename__ = "permissions"

    permission: Mapped[str] = mapped_column(String(80), primary_key=True)
    granted: Mapped[bool] = mapped_column(Boolean, default=True)
    decided_by: Mapped[str] = mapped_column(String(20), default="built-in")  # built-in | user
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
