"""Jira connector.

One issue becomes one document: summary, description, and every comment in
order. Comments are the point — the decision that explains a ticket is almost
never in its description, it is in the fourth reply three weeks later.

Two API generations are in play. Jira's v2 search returns descriptions as plain
wiki text, which is exactly what we want; v3 returns Atlassian Document Format,
a block tree. This tries v2 first and falls back to v3's `/search/jql` with an
ADF flattener when an instance has retired v2, so it works on both.

Incrementality is a JQL `updated >=` window. JQL's time resolution is a minute,
so the window is deliberately re-opened one minute early on each sync —
overlapping is free (the content hash skips unchanged issues), missing an issue
is not.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from atlas import repo
from atlas.config import settings
from atlas.connectors import enqueue_document

log = logging.getLogger("atlas.connectors.jira")

PAGE_SIZE = 50
MAX_PAGES = 60
MAX_COMMENTS = 60

FIELDS = (
    "summary,description,status,issuetype,priority,labels,updated,"
    "comment,assignee,reporter"
)


class JiraError(RuntimeError):
    pass


class JiraConnector:
    kind = "jira"

    async def sync(self, *, tenant_id: UUID, source: dict[str, Any]) -> int:
        config = source["config"]
        s = settings()
        base = str(config.get("base_url") or s.jira_base_url or "").rstrip("/")
        email = config.get("email") or s.jira_email
        token = config.get("token") or s.jira_api_token
        if not (base and email and token):
            raise ValueError(
                "Jira needs base_url, email and an API token. Put them in "
                "source.config, or set JIRA_BASE_URL, JIRA_EMAIL and "
                "JIRA_API_TOKEN in .env.local."
            )

        since: str | None = (source["cursor"] or {}).get("updated")
        jql = _jql(config.get("project"), since, config.get("jql"))

        seen = 0
        newest = since

        async with httpx.AsyncClient(
            base_url=base, auth=(str(email), str(token)), timeout=60
        ) as http:
            for issue in await self._search(http, jql):
                fields = issue.get("fields") or {}
                key = issue.get("key") or issue["id"]
                updated = fields.get("updated") or ""
                if updated and (newest is None or updated > newest):
                    newest = updated

                await enqueue_document(
                    tenant_id=tenant_id,
                    source=source,
                    external_id=key,
                    title=f"{key} · {fields.get('summary') or 'Untitled'}",
                    kind="md",
                    text=_render(key, fields),
                    uri=f"{base}/browse/{key}",
                    metadata={
                        "status": (fields.get("status") or {}).get("name"),
                        "type": (fields.get("issuetype") or {}).get("name"),
                        "updated": updated,
                        "labels": fields.get("labels") or [],
                    },
                )
                seen += 1

        if newest:
            # Store the raw ISO stamp; _jql rewinds it to a minute boundary.
            await repo.set_source_cursor(source["id"], {"updated": newest})
        return seen

    async def _search(self, http: httpx.AsyncClient, jql: str) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        start = 0
        next_token: str | None = None
        legacy = True

        for _ in range(MAX_PAGES):
            if legacy:
                res = await http.get(
                    "/rest/api/2/search",
                    params={
                        "jql": jql,
                        "startAt": start,
                        "maxResults": PAGE_SIZE,
                        "fields": FIELDS,
                    },
                )
                if res.status_code in (404, 410):
                    # This instance has retired v2. Start over on v3.
                    log.info("jira v2 search unavailable — falling back to v3")
                    legacy = False
                    continue
            else:
                res = await http.get(
                    "/rest/api/3/search/jql",
                    params={
                        "jql": jql,
                        "maxResults": PAGE_SIZE,
                        "fields": FIELDS,
                        **({"nextPageToken": next_token} if next_token else {}),
                    },
                )

            if res.status_code >= 400:
                raise JiraError(
                    f"jira search failed ({res.status_code}): {res.text[:200]}"
                )

            body = res.json()
            batch = body.get("issues") or []
            issues.extend(batch)

            if legacy:
                start += len(batch)
                if not batch or start >= int(body.get("total") or 0):
                    break
            else:
                next_token = body.get("nextPageToken")
                if body.get("isLast") or not next_token:
                    break

        return issues


# ────────────────────────────────────────────────────────────────  helpers ──


def _jql(project: str | None, since: str | None, extra: str | None) -> str:
    clauses: list[str] = []
    if project:
        clauses.append(f'project = "{project}"')
    if since:
        clauses.append(f'updated >= "{_minute(since)}"')
    if extra:
        clauses.append(f"({extra})")
    if not clauses:
        return "ORDER BY updated ASC"
    return " AND ".join(clauses) + " ORDER BY updated ASC"


def _minute(iso: str) -> str:
    """JQL wants `yyyy/MM/dd HH:mm`, plus a minute of overlap for safety."""
    try:
        stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return (stamp.astimezone(UTC) - timedelta(minutes=1)).strftime("%Y/%m/%d %H:%M")


def _text(value: Any) -> str:
    """Descriptions and comment bodies are plain text on v2, ADF on v3."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _adf(value).strip()
    return str(value).strip()


def _adf(node: dict[str, Any]) -> str:
    """Flattens Atlassian Document Format to something a chunker can read."""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"

    children = "".join(
        _adf(c) for c in node.get("content") or [] if isinstance(c, dict)
    )

    if kind in {"paragraph", "heading"}:
        level = (node.get("attrs") or {}).get("level")
        prefix = "#" * int(level) + " " if kind == "heading" and level else ""
        return f"\n{prefix}{children}\n"
    if kind == "listItem":
        return f"- {children.strip()}\n"
    if kind == "codeBlock":
        return f"\n```\n{children}\n```\n"
    if kind == "blockquote":
        return f"\n> {children.strip()}\n"
    if kind == "rule":
        return "\n---\n"
    return children


def _render(key: str, fields: dict[str, Any]) -> str:
    """The issue as Markdown, comments kept in order under one heading."""
    status = (fields.get("status") or {}).get("name") or "unknown"
    issue_type = (fields.get("issuetype") or {}).get("name") or "issue"
    assignee = (fields.get("assignee") or {}).get("displayName") or "unassigned"
    reporter = (fields.get("reporter") or {}).get("displayName") or "unknown"
    labels = ", ".join(fields.get("labels") or []) or "none"

    parts = [
        f"# {key} · {fields.get('summary') or 'Untitled'}",
        "",
        f"{issue_type} · {status} · assigned to {assignee} · reported by "
        f"{reporter} · labels: {labels}",
        "",
        "## Description",
        _text(fields.get("description")) or "_No description._",
    ]

    comments = ((fields.get("comment") or {}).get("comments") or [])[:MAX_COMMENTS]
    if comments:
        parts += ["", "## Comments"]
        for comment in comments:
            who = (comment.get("author") or {}).get("displayName") or "unknown"
            when = (comment.get("created") or "")[:16].replace("T", " ")
            body = _text(comment.get("body"))
            if body:
                parts += ["", f"**{who}** · {when}", body]

    return "\n".join(parts)
