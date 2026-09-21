"""Development seed:  uv run atlas-seed   (run from api/)

Creates a tenant, three groups with deliberately different reach, and four users
— so the permission filter can be *seen* working rather than taken on trust. Ask
the same question as alice and as carol and the retrieved passages differ; that
is the whole point of the ACL living inside the retrieval scan.

Idempotent: safe to re-run.
"""

from __future__ import annotations

import asyncio
import os
import sys

from atlas import repo
from atlas.db import close_pool
from atlas.passwords import hash_password

TENANT_SLUG = "default"
TENANT_NAME = "Acme"

#: Every seeded user gets the same password, because the point of these four
#: accounts is to switch between them quickly and watch the corpus change. It is
#: a development fixture and the printout below says so.
PASSWORD = os.getenv("ATLAS_SEED_PASSWORD", "atlas-demo-2026")

GROUPS = {
    "engineering": "Engineering",
    "hr": "People & HR",
    "public": "Company-wide",
}

# email -> (name, groups, is_admin)
#
# acme.example rather than acme.test: EmailStr refuses special-use TLDs, so a
# .test address validates nowhere and these accounts could never sign in.
USERS: dict[str, tuple[str, list[str], bool]] = {
    "alice@acme.example": ("Alice (engineering)", ["engineering", "public"], False),
    "bob@acme.example": (
        "Bob (engineering + HR)",
        ["engineering", "hr", "public"],
        False,
    ),
    "carol@acme.example": ("Carol (public only)", ["public"], False),
    "admin@acme.example": ("Admin", ["engineering", "hr", "public"], True),
}


async def main() -> int:
    tenant_id = await repo.ensure_tenant(TENANT_SLUG, TENANT_NAME)
    print(f"tenant  {TENANT_SLUG} ({tenant_id})")

    group_ids = {}
    for key, name in GROUPS.items():
        group_ids[key] = await repo.ensure_group(tenant_id, key, name)
        print(f"group   {key}")

    # Hashed once: scrypt is deliberately slow, and four identical passwords do
    # not need four derivations.
    password_hash = hash_password(PASSWORD)

    for email, (name, groups, is_admin) in USERS.items():
        user_id = await repo.ensure_user(tenant_id, email, name, is_admin)
        for key in groups:
            await repo.add_user_to_group(user_id, group_ids[key])
        await repo.set_password(user_id, password_hash)
        flag = " [admin]" if is_admin else ""
        print(f"user    {email} -> {', '.join(groups)}{flag}")

    print(
        f"\nSign in at /signin with any address above, password {PASSWORD!r}.\n"
        "Ask the same question as alice@ and as carol@ — same query, different "
        "corpus, because the ACL sits inside the retrieval scan.\n"
        "Set ATLAS_SEED_PASSWORD before running this to choose your own."
    )
    return 0


def run() -> None:
    code = 1
    try:
        code = asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 — the message is the point
        print(f"seed failed: {exc}", file=sys.stderr)
    finally:
        asyncio.run(close_pool())
    raise SystemExit(code)


if __name__ == "__main__":
    run()
