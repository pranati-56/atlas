"""Request-scoped dependencies.

Authentication resolves a request to a `Principal` — a tenant, a user, and the
set of group keys that user actually holds. Retrieval takes the `Principal`, not
a user id, so there is no code path where a query runs without the permission
filter having already been resolved.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from atlas import repo
from atlas.config import settings
from atlas.repo import Principal

log = logging.getLogger("atlas.auth")

SESSION_COOKIE = "atlas_session"
SESSION_TTL_S = 60 * 60 * 24 * 14


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_session(tenant_slug: str, email: str) -> str:
    """Signed, stateless session cookie.

    Stateless because the alternative — a sessions table — buys revocation this
    system does not yet need, at the cost of a database round trip on every
    request including the streaming ones.
    """
    secret = settings().session_secret
    if not secret:
        raise RuntimeError(
            "ATLAS_SESSION_SECRET is not set. Generate one with: "
            "openssl rand -base64 48"
        )
    payload = json.dumps(
        {"t": tenant_slug, "e": email, "x": int(time.time()) + SESSION_TTL_S},
        separators=(",", ":"),
    ).encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def read_session(token: str) -> tuple[str, str] | None:
    secret = settings().session_secret
    if not secret or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    try:
        payload = _unb64(body)
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
        # compare_digest, not ==: a timing-variable comparison on a signature is
        # a real forgery oracle, and cheap to avoid.
        if not hmac.compare_digest(_unb64(sig), expected):
            return None
        claims = json.loads(payload)
        if int(claims.get("x", 0)) < time.time():
            return None
        return str(claims["t"]), str(claims["e"])
    except (ValueError, KeyError):
        return None


async def current_principal(
    request: Request,
    x_atlas_tenant: Annotated[str | None, Header()] = None,
) -> Principal:
    cfg = settings()
    identity: tuple[str, str] | None = None

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        identity = read_session(token)

    if identity is None and cfg.dev_user:
        # Development convenience. Loud on purpose: this bypasses the login
        # entirely, and silently doing so in a deployed environment is how a
        # corpus leaks.
        log.warning("ATLAS_DEV_USER is set — authenticating as %s", cfg.dev_user)
        identity = (x_atlas_tenant or "default", cfg.dev_user)

    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not signed in.",
            headers={"www-authenticate": "Cookie"},
        )

    tenant_slug, email = identity
    principal = await repo.load_principal(tenant_slug, email)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"No user {email!r} in tenant {tenant_slug!r}.",
        )
    return principal


async def require_admin(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This operation requires an admin.",
        )
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
AdminPrincipal = Annotated[Principal, Depends(require_admin)]
