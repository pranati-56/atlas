"""Auth, in the parts that need no database.

The routes themselves want an integration test against a live Postgres, which
does not exist yet. What is testable here is the machinery underneath, and it is
the machinery worth being sure about: a password check that passes on a wrong
password, or a session token that still verifies after being tampered with, are
both silent failures.
"""

from __future__ import annotations

import time

import pytest

from atlas import deps
from atlas.config import Settings
from atlas.passwords import (
    DUMMY_HASH,
    hash_password,
    needs_rehash,
    verify_password,
    waste_time,
)
from atlas.routers.auth import (
    MAX_ATTEMPTS,
    _attempts,
    _record_failure,
    _slugify,
    _throttled,
)

DSN = "postgresql://u:p@localhost:5432/atlas"


@pytest.fixture(autouse=True)
def _clean_attempts() -> None:
    _attempts.clear()


# ──────────────────────────────────────────────────────────────── passwords ──


def test_a_password_verifies_and_a_near_miss_does_not() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("correct horse battery stapl", stored)
    assert not verify_password("Correct horse battery staple", stored)
    assert not verify_password("", stored)


def test_the_same_password_hashes_differently_every_time() -> None:
    # A shared salt would let one rainbow table cover every account at once.
    a = hash_password("same-password-twice")
    b = hash_password("same-password-twice")
    assert a != b
    assert verify_password("same-password-twice", a)
    assert verify_password("same-password-twice", b)


def test_a_null_or_broken_hash_reads_as_wrong_not_as_an_error() -> None:
    # A user row with no password — seeded, or invited and not yet accepted —
    # must fail closed rather than raise a 500 that says "you found something".
    assert not verify_password("anything", None)
    assert not verify_password("anything", "")
    assert not verify_password("anything", "not-a-hash")
    assert not verify_password("anything", "scrypt$bad$params$here$x$y")
    assert not verify_password("anything", "argon2$v=19$m=1$whatever")


def test_the_stored_format_carries_its_own_parameters() -> None:
    stored = hash_password("parameterised")
    prefix, n, r, p, salt, digest = stored.split("$")
    assert prefix == "scrypt"
    assert (int(n), int(r), int(p)) == (16384, 8, 1)
    assert salt and digest

    assert not needs_rehash(stored)
    # Written under a weaker cost: login upgrades it in place.
    assert needs_rehash("scrypt$1024$8$1$YQ==$Yg==")
    assert needs_rehash("bcrypt$whatever")
    assert not needs_rehash(None)


def test_an_unknown_account_still_costs_time() -> None:
    # Sign-in must not answer "no such user" faster than "wrong password", or
    # the form becomes a membership oracle for the workspace.
    started = time.perf_counter()
    waste_time()
    elapsed = time.perf_counter() - started
    assert elapsed > 0.001, "the dummy verification was optimised away"
    assert DUMMY_HASH.startswith("scrypt$")


# ─────────────────────────────────────────────────────────────── throttling ──


def test_repeated_failures_lock_the_address_out() -> None:
    email = "guessme@acme.example"
    for _ in range(MAX_ATTEMPTS - 1):
        _record_failure(email)
    assert not _throttled(email), "locked out too early"

    _record_failure(email)
    assert _throttled(email), "the lockout never engaged"


def test_a_successful_sign_in_clears_the_count() -> None:
    email = "typo@acme.example"
    for _ in range(MAX_ATTEMPTS - 1):
        _record_failure(email)
    _attempts.pop(email, None)  # what login() does on success
    assert not _throttled(email)


def test_one_address_being_locked_does_not_lock_another() -> None:
    for _ in range(MAX_ATTEMPTS):
        _record_failure("victim@acme.example")
    assert _throttled("victim@acme.example")
    assert not _throttled("bystander@acme.example")


# ──────────────────────────────────────────────────────────────────── slugs ──


def test_workspace_names_become_usable_slugs() -> None:
    assert _slugify("Acme") == "acme"
    assert _slugify("Acme Corp") == "acme-corp"
    assert _slugify("  Acme, Inc.  ") == "acme-inc"
    assert _slugify("R&D — Platform") == "r-d-platform"
    # A name with nothing slug-shaped in it still has to yield something the
    # URL and the session payload can carry.
    assert _slugify("!!!") == "workspace"
    assert _slugify("日本語") == "workspace"


# ────────────────────────────────────────────────────────────────── session ──


def _with_secret(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    """Point the session codec at a known signing key.

    Patched on `deps`, not on `config`: deps.py does `from atlas.config import
    settings`, which binds the function object at import time, so replacing it
    on the config module would never be seen here.
    """
    monkeypatch.setattr(
        deps,
        "settings",
        lambda: Settings(  # type: ignore[call-arg]
            DATABASE_URL=DSN, ATLAS_SESSION_SECRET=secret
        ),
    )


def test_a_session_round_trips_and_a_tampered_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_secret(monkeypatch, "test-secret-not-a-real-one")

    token = deps.issue_session("acme", "alice@acme.example")
    assert deps.read_session(token) == ("acme", "alice@acme.example")

    # Flip a character in the payload. The signature must stop matching.
    body, sig = token.rsplit(".", 1)
    forged = f"{body[:-1]}{'A' if body[-1] != 'A' else 'B'}.{sig}"
    assert deps.read_session(forged) is None

    # A token signed with our key must not validate under a different one.
    _with_secret(monkeypatch, "a-completely-different-secret")
    assert deps.read_session(token) is None


def test_a_session_with_no_secret_configured_never_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_secret(monkeypatch, "")
    # Fails closed: no secret means no signature worth trusting.
    assert deps.read_session("anything.at-all") is None


def test_an_expired_session_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_secret(monkeypatch, "test-secret-not-a-real-one")
    token = deps.issue_session("acme", "alice@acme.example")

    # Fourteen days and a second later. The value is captured *before* the
    # patch: `deps.time` is the same module object as ours, so a lambda that
    # called time.time() would be calling itself.
    later = time.time() + deps.SESSION_TTL_S + 1
    monkeypatch.setattr(deps.time, "time", lambda: later)
    assert deps.read_session(token) is None
