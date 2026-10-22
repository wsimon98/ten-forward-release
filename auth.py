"""
auth.py — who is listening.

Out of the box there is one person, `admin`, with no password, and nobody is ever asked anything: the
middleware signs that person in on every request. The moment a second person exists, or anybody sets a
password, the web page asks who is listening and the phone app asks for a name (and password if it has one).

Browsers carry a cookie (`tf_session`), the Android app an `Authorization: Bearer <token>` header, and
`<audio>`/ExoPlayer — which cannot set headers — may pass `?token=` on /media and /app URLs only.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request

import config
from db import DB, new_id

LOG = logging.getLogger("tenforward.auth")

COOKIE = "tf_session"
SESSION_DAYS = 365
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 64}

# paths anybody may reach without being signed in
PUBLIC_EXACT = {"/", "/favicon.ico", "/sw.js", "/manifest.webmanifest", "/api/hello", "/api/status", "/listen",
                "/api/auth/login", "/api/auth/logout", "/api/auth/users-public", "/api/auth/me", "/api/client/log",
                "/api/app/version"}   # the phone asks about updates before it is signed in
PUBLIC_PREFIX = ("/static/", "/app/", "/docs", "/openapi.json", "/redoc", "/listen/")   # /listen/ is the public dial
# where a ?token= query is accepted (media players cannot send headers)
TOKEN_QUERY_PREFIX = ("/media/", "/app/", "/api/voices/")

_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,32}$")


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> tuple[str, str]:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return digest.hex(), salt.hex()


def check_password(password: str, password_hash: str | None, salt: str | None) -> bool:
    if not password_hash or not salt:
        return False
    try:
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), **_SCRYPT)
    except Exception:
        return False
    return hmac.compare_digest(digest.hex(), password_hash)


def public_user(u: dict[str, Any] | None) -> dict[str, Any] | None:
    if not u:
        return None
    return {"id": u["id"], "name": u["name"], "role": u.get("role") or "user", "has_password": bool(u.get("password_hash"))}


# --------------------------------------------------------------------------- users
def all_users(db: DB) -> list[dict[str, Any]]:
    return db.query("users", order="created ASC")


def user_by_name(db: DB, name: str) -> dict[str, Any] | None:
    rows = db.query("users", "LOWER(name)=?", ((name or "").strip().lower(),), order="created ASC", limit=1)
    return rows[0] if rows else None


def bootstrap(db: DB) -> dict[str, Any]:
    """First start: one admin, no password. Nothing to set up, nothing to remember."""
    users = all_users(db)
    if users:
        return users[0]
    user = {"id": new_id("usr_"), "name": "admin", "password_hash": None, "salt": None, "role": "admin",
            "created": time.time(), "last_seen": None}
    db.insert("users", user)
    LOG.info("users: created admin (no password)")
    return user


def create_user(db: DB, name: str, password: str | None = None, role: str = "user") -> dict[str, Any]:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("a name is 1 to 32 letters, numbers, spaces, dots, dashes or underscores")
    if user_by_name(db, name):
        raise ValueError(f"{name} already listens here")
    role = "admin" if role == "admin" else "user"
    ph, salt = hash_password(password) if password else (None, None)
    user = {"id": new_id("usr_"), "name": name, "password_hash": ph, "salt": salt, "role": role,
            "created": time.time(), "last_seen": None}
    db.insert("users", user)
    LOG.info("users: added %s (%s, %s)", name, role, "password" if password else "no password")
    return user


def set_password(db: DB, user_id: str, password: str | None) -> dict[str, Any]:
    user = db.get("users", user_id)
    if not user:
        raise ValueError("no such person")
    if password:
        ph, salt = hash_password(password)
    else:
        ph, salt = None, None
    db.update("users", user_id, password_hash=ph, salt=salt)
    LOG.info("users: %s password %s", user["name"], "set" if password else "removed")
    return db.get("users", user_id)


def set_role(db: DB, user_id: str, role: str) -> dict[str, Any]:
    user = db.get("users", user_id)
    if not user:
        raise ValueError("no such person")
    role = "admin" if role == "admin" else "user"
    if role != "admin" and user.get("role") == "admin" and _admin_count(db) <= 1:
        raise ValueError("somebody has to stay admin")
    db.update("users", user_id, role=role)
    return db.get("users", user_id)


def _admin_count(db: DB) -> int:
    return db.count("users", "role='admin'")


def delete_user(db: DB, user_id: str) -> None:
    user = db.get("users", user_id)
    if not user:
        raise ValueError("no such person")
    if user.get("role") == "admin" and _admin_count(db) <= 1:
        raise ValueError("this is the last admin")
    for s in db.query("sessions", "user_id=?", (user_id,), order="created DESC"):
        db.delete("sessions", s["token"])
    db.delete("users", user_id)
    LOG.info("users: removed %s", user["name"])


# --------------------------------------------------------------------------- sessions
def start_session(db: DB, user: dict[str, Any], device: str = "") -> str:
    token = secrets.token_urlsafe(32)
    db.insert("sessions", {"token": token, "user_id": user["id"], "device": (device or "")[:120],
                           "created": time.time(), "last_seen": time.time()})
    db.update("users", user["id"], last_seen=time.time())
    return token


def end_session(db: DB, token: str) -> None:
    if token:
        db.delete("sessions", token)


def user_for_token(db: DB, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    s = db.get("sessions", token)
    if not s:
        return None
    if time.time() - float(s.get("created") or 0) > SESSION_DAYS * 86400:
        db.delete("sessions", token)
        return None
    user = db.get("users", s.get("user_id") or "")
    if not user:
        db.delete("sessions", token)
        return None
    if time.time() - float(s.get("last_seen") or 0) > 300:
        db.update("sessions", token, last_seen=time.time())
        db.update("users", user["id"], last_seen=time.time())
    return user


def login(db: DB, name: str, password: str | None = None, device: str = "") -> tuple[dict[str, Any], str]:
    user = user_by_name(db, name)
    if not user:
        raise ValueError("nobody listens here by that name")
    if user.get("password_hash"):
        if not password or not check_password(password, user.get("password_hash"), user.get("salt")):
            raise ValueError("that password does not match")
    return user, start_session(db, user, device)


# --------------------------------------------------------------------------- policy
def login_required(db: DB) -> bool:
    """False only in the simple case: one person, no password, and the setting is off."""
    if config.cfg("require_login"):
        return True
    users = all_users(db)
    if len(users) != 1:
        return True
    return bool(users[0].get("password_hash"))


def auto_user(db: DB) -> dict[str, Any] | None:
    if login_required(db):
        return None
    users = all_users(db)
    return users[0] if users else None


def public_users(db: DB) -> list[dict[str, Any]]:
    """Names for the "who is listening" sheet. Empty when nobody has to sign in."""
    if not login_required(db):
        return []
    return [{"name": u["name"], "needs_password": bool(u.get("password_hash")), "role": u.get("role") or "user"}
            for u in all_users(db)]


def is_public_path(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX)


def token_from_request(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    tok = request.cookies.get(COOKIE)
    if tok:
        return tok
    q = request.query_params.get("token")
    if q and request.url.path.startswith(TOKEN_QUERY_PREFIX):
        return q
    return ""


def resolve(db: DB, request: Request) -> dict[str, Any] | None:
    """The person behind this request: their session, or the automatic single user."""
    user = user_for_token(db, token_from_request(request))
    if user:
        return user
    return auto_user(db)


def current(request: Request) -> dict[str, Any] | None:
    return getattr(request.state, "user", None)


def require_user(request: Request) -> dict[str, Any]:
    user = current(request)
    if not user:
        raise HTTPException(401, "login")
    return user


def admin_only(request: Request) -> dict[str, Any]:
    """FastAPI dependency: `dependencies=[Depends(auth.admin_only)]` on a route only an admin may use."""
    user = require_user(request)
    if (user.get("role") or "user") != "admin":
        raise HTTPException(403, "only an admin can do that")
    return user


def is_admin(request: Request) -> bool:
    user = current(request)
    return bool(user and (user.get("role") or "user") == "admin")


def cookie_kwargs(request: Request) -> dict[str, Any]:
    secure = (request.url.scheme == "https") or (request.headers.get("x-forwarded-proto") == "https")
    return {"key": COOKIE, "httponly": True, "samesite": "lax", "secure": secure, "max_age": SESSION_DAYS * 86400, "path": "/"}
