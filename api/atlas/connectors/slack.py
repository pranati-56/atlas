"""Slack connector.

Slack's unit of meaning is the **thread**, not the message. A reply that reads
"yes, ship it" is worthless alone and decisive next to the question above it, so
this connector never emits a message as a document. It emits:

  - one document per thread (parent plus every reply), and
  - one document per channel-day for everything that was never threaded.

Both reach the pipeline as `conversation`, which keeps threads whole up to the
chunk ceiling and splits on message boundaries rather than mid-message.

Incrementality is a per-channel timestamp cursor, rewound a day before each
sync. The rewind is deliberate: today's channel-day document is still being
written to, so re-reading it is the only way it ends up complete. Re-reading is
nearly free — the ingest pipeline hashes content and skips documents whose text
has not changed.

Known limit: a new reply on an old thread is not detected, because
`conversations.history` orders by the parent's timestamp and the parent has not
moved. Threads stay current for as long as the sync interval; older ones are
picked up on a full re-sync (clear the source's cursor).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from atlas import repo
from atlas.config import settings
from atlas.connectors import enqueue_document

log = logging.getLogger("atlas.connectors.slack")

API = "https://slack.com/api"

#: How far to rewind the per-channel cursor. Long enough that the current
#: channel-day document is always re-read whole, short enough to stay cheap.
REWIND_SECONDS = 26 * 3600

#: Slack's Web API is tier-limited; most of these methods sit near 50 req/min.
#: A small gap between calls keeps a large workspace under the limit without
#: relying on 429 handling for the common case.
PACE_SECONDS = 0.25

MAX_PAGES = 40

#: Joins, leaves, pins and topic notices. Noise in retrieval, and they dominate
#: a quiet channel's message count.
_SKIP_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "pinned_item",
    "unpinned_item",
    "bot_add",
    "bot_remove",
}

_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_REF = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]*))?>")
_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")


class SlackError(RuntimeError):
    pass


class SlackConnector:
    kind = "slack"

    async def sync(self, *, tenant_id: UUID, source: dict[str, Any]) -> int:
        config = source["config"]
        token: str | None = config.get("token") or settings().slack_bot_token
        if not token:
            raise ValueError(
                "No Slack token: set SLACK_BOT_TOKEN or put one in "
                "source.config.token. A bot token (xoxb-) with channels:read, "
                "channels:history and users:read covers public channels."
            )

        wanted: set[str] = {
            c.strip() for c in (config.get("channels") or []) if str(c).strip()
        }
        cursors: dict[str, str] = dict((source["cursor"] or {}).get("channels") or {})
        seen = 0

        async with httpx.AsyncClient(
            base_url=API,
            headers={"authorization": f"Bearer {token}"},
            timeout=45,
        ) as http:
            users = await self._users(http)
            channels = await self._channels(http)

            for channel in channels:
                cid = channel["id"]
                name = channel.get("name") or cid

                # A channel name in config is friendlier than an ID; take either.
                if wanted and cid not in wanted and name not in wanted:
                    continue
                # History on a channel the bot has not joined returns
                # not_in_channel. Skipping is quieter than failing the sync.
                if not channel.get("is_member"):
                    log.info("skipping #%s — the bot is not a member", name)
                    continue

                messages = await self._history(http, cid, _rewind(cursors.get(cid)))
                if not messages:
                    continue

                latest = max(float(m["ts"]) for m in messages)
                seen += await self._emit(
                    http,
                    tenant_id=tenant_id,
                    source=source,
                    channel_id=cid,
                    channel_name=name,
                    messages=messages,
                    users=users,
                )
                cursors[cid] = f"{latest:.6f}"

        await repo.set_source_cursor(source["id"], {"channels": cursors})
        return seen

    # ── enumeration ──────────────────────────────────────────────────────

    async def _call(
        self, http: httpx.AsyncClient, method: str, **params: Any
    ) -> dict[str, Any]:
        for attempt in range(4):
            res = await http.get(f"/{method}", params=params)
            if res.status_code == 429:
                # Slack's own back-off, not ours to guess.
                delay = float(res.headers.get("retry-after") or 2)
                log.info("slack rate limited on %s — waiting %.0fs", method, delay)
                await asyncio.sleep(delay)
                continue
            res.raise_for_status()
            body = res.json()
            if body.get("ok"):
                await asyncio.sleep(PACE_SECONDS)
                return body
            error = body.get("error", "unknown_error")
            if error == "ratelimited":
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise SlackError(f"slack {method} failed: {error}")
        raise SlackError(f"slack {method} kept rate limiting")

    async def _paged(
        self, http: httpx.AsyncClient, method: str, key: str, **params: Any
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            body = await self._call(
                http, method, **params, **({"cursor": cursor} if cursor else {})
            )
            out.extend(body.get(key) or [])
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        return out

    async def _users(self, http: httpx.AsyncClient) -> dict[str, str]:
        """id → display name, so transcripts read as prose rather than as IDs."""
        try:
            members = await self._paged(http, "users.list", "members", limit=200)
        except (SlackError, httpx.HTTPError) as exc:
            # users:read is a separate scope. Losing names is a cosmetic
            # downgrade, not a reason to abandon the whole sync.
            log.warning("could not list users (%s) — falling back to raw IDs", exc)
            return {}
        names: dict[str, str] = {}
        for m in members:
            profile = m.get("profile") or {}
            names[m["id"]] = (
                profile.get("display_name")
                or profile.get("real_name")
                or m.get("name")
                or m["id"]
            )
        return names

    async def _channels(self, http: httpx.AsyncClient) -> list[dict[str, Any]]:
        return await self._paged(
            http,
            "conversations.list",
            "channels",
            types="public_channel",
            exclude_archived="true",
            limit=200,
        )

    async def _history(
        self, http: httpx.AsyncClient, channel: str, oldest: str | None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"channel": channel, "limit": 200}
        if oldest:
            params["oldest"] = oldest
        try:
            messages = await self._paged(
                http, "conversations.history", "messages", **params
            )
        except SlackError as exc:
            log.warning("history unavailable for %s: %s", channel, exc)
            return []
        keep = [m for m in messages if _worth_indexing(m)]
        keep.sort(key=lambda m: float(m["ts"]))
        return keep

    # ── emission ─────────────────────────────────────────────────────────

    async def _emit(
        self,
        http: httpx.AsyncClient,
        *,
        tenant_id: UUID,
        source: dict[str, Any],
        channel_id: str,
        channel_name: str,
        messages: list[dict[str, Any]],
        users: dict[str, str],
    ) -> int:
        days: dict[str, list[dict[str, Any]]] = {}
        emitted = 0

        for message in messages:
            if int(message.get("reply_count") or 0) > 0:
                thread = await self._thread(http, channel_id, message["ts"])
                if len(thread) < 2:
                    # Replies were deleted or filtered out; treat the parent as
                    # an ordinary message rather than emit a thread of one.
                    days.setdefault(_day(message["ts"]), []).append(message)
                    continue
                await self._emit_thread(
                    tenant_id=tenant_id,
                    source=source,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    thread=thread,
                    users=users,
                )
                emitted += 1
            elif message.get("thread_ts") and message["thread_ts"] != message["ts"]:
                # A stray reply surfaced without its parent. Its thread is
                # emitted whole when the parent is processed.
                continue
            else:
                days.setdefault(_day(message["ts"]), []).append(message)

        for day, bucket in days.items():
            await self._emit_day(
                tenant_id=tenant_id,
                source=source,
                channel_id=channel_id,
                channel_name=channel_name,
                day=day,
                bucket=bucket,
                users=users,
            )
            emitted += 1

        return emitted

    async def _thread(
        self, http: httpx.AsyncClient, channel: str, ts: str
    ) -> list[dict[str, Any]]:
        try:
            replies = await self._paged(
                http,
                "conversations.replies",
                "messages",
                channel=channel,
                ts=ts,
                limit=200,
            )
        except SlackError as exc:
            log.warning("replies unavailable for %s/%s: %s", channel, ts, exc)
            return []
        keep = [m for m in replies if _worth_indexing(m)]
        keep.sort(key=lambda m: float(m["ts"]))
        return keep

    async def _emit_thread(
        self,
        *,
        tenant_id: UUID,
        source: dict[str, Any],
        channel_id: str,
        channel_name: str,
        thread: list[dict[str, Any]],
        users: dict[str, str],
    ) -> None:
        parent = thread[0]
        title = f"#{channel_name} · {_subject(_clean(parent.get('text') or '', users))}"
        body = "\n".join([f"# {title}", *(_line(m, users) for m in thread)])
        await enqueue_document(
            tenant_id=tenant_id,
            source=source,
            external_id=f"{channel_id}/{parent['ts']}",
            title=title,
            kind="conversation",
            text=body,
            uri=_permalink(channel_id, parent["ts"]),
            metadata={
                "channel": channel_name,
                "channel_id": channel_id,
                "thread_ts": parent["ts"],
                "message_count": len(thread),
            },
        )

    async def _emit_day(
        self,
        *,
        tenant_id: UUID,
        source: dict[str, Any],
        channel_id: str,
        channel_name: str,
        day: str,
        bucket: list[dict[str, Any]],
        users: dict[str, str],
    ) -> None:
        title = f"#{channel_name} · {day}"
        body = "\n".join([f"# {title}", *(_line(m, users) for m in bucket)])
        await enqueue_document(
            tenant_id=tenant_id,
            source=source,
            external_id=f"{channel_id}/day/{day}",
            title=title,
            kind="conversation",
            text=body,
            uri=_permalink(channel_id, bucket[0]["ts"]),
            metadata={
                "channel": channel_name,
                "channel_id": channel_id,
                "day": day,
                "message_count": len(bucket),
            },
        )


# ────────────────────────────────────────────────────────────────  helpers ──


def _worth_indexing(message: dict[str, Any]) -> bool:
    if message.get("subtype") in _SKIP_SUBTYPES:
        return False
    # A bare attachment or reaction carries no retrievable text.
    return len((message.get("text") or "").strip()) >= 3


def _rewind(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        return f"{max(0.0, float(cursor) - REWIND_SECONDS):.6f}"
    except ValueError:
        return None


def _day(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d")


def _stamp(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d %H:%M")


def _permalink(channel: str, ts: str) -> str:
    # Workspace-agnostic form: slack.com redirects to the right workspace for
    # whoever clicks, which is what we want when the domain is not in config.
    return f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"


def _clean(text: str, users: dict[str, str]) -> str:
    """Slack markup → something a retriever and a reader can both use."""
    text = _MENTION.sub(lambda m: f"@{users.get(m.group(1), m.group(1))}", text)
    text = _CHANNEL_REF.sub(lambda m: f"#{m.group(2) or m.group(1)}", text)
    text = _LINK.sub(lambda m: m.group(2) or m.group(1), text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    # Newlines inside a message would read as separate messages downstream —
    # the conversation chunker splits on lines.
    return " ".join(text.split())


def _line(message: dict[str, Any], users: dict[str, str]) -> str:
    who = users.get(message.get("user") or "", message.get("username") or "unknown")
    return f"[{_stamp(message['ts'])}] {who}: {_clean(message.get('text') or '', users)}"


def _subject(text: str, limit: int = 70) -> str:
    """A thread has no title, so its first message stands in for one."""
    if not text:
        return "thread"
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"
