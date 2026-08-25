"""Password hashing.

scrypt from the standard library rather than argon2 or bcrypt from PyPI. Both of
those are better-known, and either would be a defensible choice, but each adds a
compiled dependency to a service whose only other native code is asyncpg. scrypt
is memory-hard, has been in `hashlib` since 3.6, and is what the stored format
below records — so moving to argon2 later is a new prefix and a rehash-on-login,
not a migration.

The stored string is self-describing:

    scrypt$16384$8$1$<salt-b64>$<hash-b64>

which means the cost can be raised without invalidating a single existing hash:
`verify` reads the parameters out of the stored value, and `needs_rehash` tells
the caller when to write a stronger one back.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

#: 16 MiB per hash (128 * N * r). High enough to make offline cracking painful,
#: low enough that a burst of sign-ins cannot exhaust a small container — the
#: web process handles these inline.
N = 2**14
R = 8
P = 1
DKLEN = 32
SALT_BYTES = 16

#: OpenSSL's own default maxmem is around 32 MiB, close enough to the figure
#: above to fail on some builds. State it rather than inherit it.
MAXMEM = 64 * 1024 * 1024

PREFIX = "scrypt"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text)


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    # NFKC would be the pedantic choice for unicode passwords; UTF-8 alone
    # matches what every other implementation here does and avoids a class of
    # "it worked on my keyboard" bugs.
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=DKLEN,
        maxmem=MAXMEM,
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt, n=N, r=R, p=P)
    return f"{PREFIX}${N}${R}${P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """False for a wrong password, a null hash, or a malformed one.

    Never raises. A corrupt row must read as "this credential does not work",
    not as a 500 that tells an attacker they found something interesting.
    """
    if not stored:
        return False
    try:
        prefix, n, r, p, salt, digest = stored.split("$")
        if prefix != PREFIX:
            return False
        expected = _unb64(digest)
        actual = _derive(password, _unb64(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError, MemoryError):
        return False
    # compare_digest, not ==: the comparison is over a secret, and a
    # timing-variable one leaks it a byte at a time.
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str | None) -> bool:
    """True when a hash was made with weaker parameters than we now use."""
    if not stored:
        return False
    try:
        prefix, n, r, p, _salt, _digest = stored.split("$")
    except ValueError:
        return True
    return prefix != PREFIX or (int(n), int(r), int(p)) != (N, R, P)


#: Verified against when the email is unknown, so a missing account costs the
#: same wall-clock time as a wrong password. Without this, sign-in is a user
#: enumeration oracle that answers in a few milliseconds.
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def waste_time() -> None:
    verify_password("not-the-password", DUMMY_HASH)
