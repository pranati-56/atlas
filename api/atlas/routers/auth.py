"""Sign in, sign up, sign out.

The session cookie and its signature already existed in `deps.py`; what was
missing was any route that issued one, so the only way into the system was
`ATLAS_DEV_USER` — a flag the code itself warns must never be set in
production. This is the door.

Two decisions worth stating.

**Sign-in asks for an email, not a workspace.** Nobody remembers a slug.
Because `(tenant_id, email)` is unique but email alone is not, one address can
hold accounts in several workspaces; when it does, this returns the list and
the client asks which. It never picks one.

**A wrong password and an unknown account are indistinguishable**, in the
response and roughly in the time taken. A sign-in form that answers "no such
user" faster than "wrong password" is a membership oracle, and for a corpus
tool the membership list is itself worth having.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field

from atlas import repo
from atlas.config import settings
from atlas.deps import SESSION_COOKIE, SESSION_TTL_S, CurrentPrincipal, issue_session
from atlas.passwords import hash_password, needs_rehash, verify_password, waste_time
from atlas.repo import Principal

log = logging.getLogger("atlas.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

MIN_PASSWORD = 10

#: Attempts allowed per address before a cool-off, and how long it lasts.
#: In-process, therefore per-worker — several replicas want this in Postgres or
#: Redis. It is here because the alternative was nothing, and nothing is an
#: unmetered password-guessing endpoint.
MAX_ATTEMPTS = 8
LOCKOUT_S = 300
_attempts: dict[str, tuple[int, float]] = {}


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    #: Sent on the second attempt, when the first returned `choices`.
    workspace: str | None = None


class SignUp(BaseModel):
    workspace: str = Field(min_length=2, max_length=60)
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD, max_length=200)
    name: str | None = Field(default=None, max_length=120)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "workspace"


def _set_cookie(response: Response, token: str) -> None:
    cfg = settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_S,
        httponly=True,  # nothing in the client reads it, so keep it from JS
        secure=cfg.cookie_secure,
        samesite=cfg.cookie_samesite,  # type: ignore[arg-type]
        domain=cfg.cookie_domain,
        path="/",
    )


def _throttled(email: str) -> bool:
    _hits, until = _attempts.get(email, (0, 0.0))
    if until > time.time():
        return True
    if until:  # the lockout expired — forget the attempts that caused it
        _attempts.pop(email, None)
    return False


def _record_failure(email: str) -> None:
    hits, _until = _attempts.get(email, (0, 0.0))
    hits += 1
    locked = time.time() + LOCKOUT_S if hits >= MAX_ATTEMPTS else 0.0
    _attempts[email] = (hits, locked)


def _payload(
    principal: Principal, slug: str, name: str | None = None
) -> dict[str, Any]:
    return {
        "email": principal.email,
        "tenant": slug,
        "tenant_name": name,
        "is_admin": principal.is_admin,
        "groups": principal.groups,
    }


@router.post("/login")
async def login(body: Credentials, response: Response) -> dict[str, Any]:
    email = body.email.strip().lower()

    if _throttled(email):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Try again in a few minutes.",
        )

    accounts = await repo.find_accounts_by_email(email)

    # Several workspaces hold this address and the caller has not said which.
    # Not an error, and not a leak: the caller supplied the address, so naming
    # the workspaces it belongs to tells them nothing they did not already have.
    if len(accounts) > 1 and not body.workspace:
        return {
            "ok": False,
            "reason": "ambiguous",
            "choices": [
                {"slug": a["tenant_slug"], "name": a["tenant_name"]} for a in accounts
            ],
        }

    account: dict[str, Any] | None = None
    if body.workspace:
        account = next(
            (a for a in accounts if a["tenant_slug"] == body.workspace), None
        )
    elif accounts:
        account = accounts[0]

    if account is None:
        # Unknown address, or not a member of that workspace. Burn the time a
        # real verification costs, then answer exactly as a wrong password does.
        waste_time()
        _record_failure(email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password.")

    if not verify_password(body.password, account["password_hash"]):
        _record_failure(email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password.")

    principal = await repo.load_principal(account["tenant_slug"], account["email"])
    if principal is None:  # pragma: no cover — the row was read a moment ago
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password.")

    # The cost was raised since this hash was written. The plaintext is in hand
    # exactly once, so this is the only moment it can be upgraded.
    if needs_rehash(account["password_hash"]):
        await repo.set_password(account["user_id"], hash_password(body.password))

    _attempts.pop(email, None)
    await repo.touch_login(account["user_id"])
    _set_cookie(response, issue_session(account["tenant_slug"], account["email"]))

    tenant = await repo.get_tenant(principal.tenant_id)
    return {
        "ok": True,
        "user": _payload(
            principal, account["tenant_slug"], tenant["name"] if tenant else None
        ),
    }


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(body: SignUp, response: Response) -> dict[str, Any]:
    cfg = settings()
    if not cfg.allow_signup:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Registration is closed on this instance. Ask an admin for an invite.",
        )

    email = body.email.strip().lower()
    base = _slugify(body.workspace)

    # Slug collisions are ordinary, not exceptional — two people can both call a
    # workspace "Acme". Suffix rather than refuse.
    slug = base
    for n in range(2, 40):
        if await repo.get_tenant_by_slug(slug) is None:
            break
        slug = f"{base}-{n}"
    else:
        raise HTTPException(status.HTTP_409_CONFLICT, "Could not name that workspace.")

    tenant_id = await repo.ensure_tenant(slug, body.workspace.strip())

    # Every new workspace gets a "public" group with its first user in it, so a
    # source marked company-wide is retrievable the moment it syncs. Without
    # this the first sign-in lands somewhere nothing can be found, and the
    # product looks broken when it is merely empty.
    public_group = await repo.ensure_group(tenant_id, "public", "Company-wide")
    user_id = await repo.ensure_user(tenant_id, email, body.name, is_admin=True)
    await repo.add_user_to_group(user_id, public_group)
    await repo.set_password(user_id, hash_password(body.password))
    await repo.touch_login(user_id)

    principal = await repo.load_principal(slug, email)
    assert principal is not None

    log.info("workspace %s created by %s", slug, email)
    _set_cookie(response, issue_session(slug, email))
    return {"ok": True, "user": _payload(principal, slug, body.workspace.strip())}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    cfg = settings()
    # Cleared with the attributes it was set with. A cookie deleted under a
    # different path, domain or samesite is a cookie that quietly survives.
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        domain=cfg.cookie_domain,
        secure=cfg.cookie_secure,
        samesite=cfg.cookie_samesite,  # type: ignore[arg-type]
        httponly=True,
    )


@router.get("/me")
async def me(principal: CurrentPrincipal) -> dict[str, Any]:
    """Who the cookie says you are. 401 when it says nothing."""
    tenant = await repo.get_tenant(principal.tenant_id)
    return _payload(
        principal,
        tenant["slug"] if tenant else "",
        tenant["name"] if tenant else None,
    )


@router.get("/config")
async def auth_config() -> dict[str, Any]:
    """What the sign-in page needs before anyone has signed in.

    `dev_user` is echoed deliberately: when the bypass is on, the page should
    say so rather than present a password form that is not guarding anything.
    """
    cfg = settings()
    return {
        "allow_signup": cfg.allow_signup,
        "dev_user": cfg.dev_user,
        "min_password": MIN_PASSWORD,
    }
