"""Everything here runs without a database, a network or a provider key.

The connectors cannot be tested end to end without live credentials, but the
parts most likely to be wrong are pure: how Slack markup becomes a transcript
line, how a Notion block becomes Markdown, how a JQL window is built, and — the
one with teeth — which config keys are allowed out of the API.
"""

from __future__ import annotations

from atlas.config import MIGRATIONS_DIR, SERVICE_ROOT, Settings
from atlas.connectors.jira import _adf, _jql, _render
from atlas.connectors.notion import _render as notion_render
from atlas.connectors.notion import _title
from atlas.connectors.slack import _clean, _line, _rewind, _subject, _worth_indexing
from atlas.routers.sources import _public

DSN = "postgresql://u:p@localhost:5432/atlas"


def _settings(**env: str) -> Settings:
    return Settings(DATABASE_URL=DSN, **env)  # type: ignore[call-arg]


# ─────────────────────────────────────────────────────────────────── config ──


def test_service_root_is_the_api_directory_not_the_repo() -> None:
    # The backend is a separate deployable; resolving to the repo root would
    # mean the frontend tree has to be present for migrations to be found.
    assert SERVICE_ROOT.name == "api"
    assert MIGRATIONS_DIR == SERVICE_ROOT / "db" / "migrations"

    # Numbered, contiguous, applied in filename order by atlas-migrate. A gap
    # means someone renamed one and the ledger no longer lines up.
    names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert [n.split("_")[0] for n in names] == [
        f"{i:03d}" for i in range(1, len(names) + 1)
    ]


def test_gemini_is_the_default_on_both_sides() -> None:
    s = _settings()
    assert s.generation_model.startswith("gemini")
    assert s.embedding_model.startswith("gemini")
    # Must match the vector(N) width in 001_core.sql or every insert fails.
    assert s.embedding_dim == 768


def test_cors_origins_parse_and_include_the_dev_server() -> None:
    # The dev server runs on 3400; allowing only 3000 fails every preflight.
    assert "http://localhost:3400" in _settings().allowed_origins

    s = _settings(ATLAS_CORS_ORIGINS="https://a.example , https://b.example,")
    assert s.allowed_origins == ["https://a.example", "https://b.example"]


# ──────────────────────────────────────────────────────────── secret filter ──


def test_no_connector_credential_escapes_the_sources_endpoint() -> None:
    source = {
        "id": "s1",
        "kind": "gdrive",
        "config": {
            "refresh_token": "REFRESH",
            "client_secret": "CLIENT",
            "token": "xoxb-BOT",
            "api_key": "KEY",
            "password": "PW",
            "client_id": "123.apps.googleusercontent.com",
            "folder_id": "F1",
            "urls": ["https://handbook.example/oncall"],
        },
    }

    out = _public(source)
    blob = repr(out)

    for secret in ("REFRESH", "CLIENT", "xoxb-BOT", "KEY", "PW"):
        assert secret not in blob, f"{secret} leaked through /sources"

    # Non-secret config still has to come back, or the UI cannot show what a
    # source is actually pointed at.
    assert out["config"]["folder_id"] == "F1"
    assert out["config"]["client_id"].endswith(".googleusercontent.com")
    assert out["has_token"] is True
    assert "refresh_token" in out["credentials"]


def test_a_source_with_no_credentials_reports_none() -> None:
    out = _public({"id": "s2", "kind": "web", "config": {"urls": ["https://x"]}})
    assert out["has_token"] is False
    assert out["credentials"] == []


# ──────────────────────────────────────────────────────────────────── slack ──


def test_slack_markup_becomes_readable_text() -> None:
    users = {"U123": "priya", "U456": "daniel"}
    raw = (
        "<@U123> asked <@U456> about <#C99|security> "
        "see <https://a.co|the doc> &amp; reply"
    )
    assert _clean(raw, users) == (
        "@priya asked @daniel about #security see the doc & reply"
    )


def test_a_message_never_spans_two_lines() -> None:
    # The conversation chunker splits on newlines, so an embedded newline would
    # read downstream as a second message from nobody.
    line = _line(
        {"ts": "1700000000.000100", "user": "U1", "text": "one\ntwo\n\nthree"},
        {"U1": "priya"},
    )
    assert "\n" not in line
    assert line.endswith("priya: one two three")


def test_noise_and_empty_messages_are_dropped() -> None:
    assert not _worth_indexing({"subtype": "channel_join", "text": "joined"})
    assert not _worth_indexing({"text": "  "})
    assert _worth_indexing({"text": "we capped sessions at 12h"})


def test_the_cursor_rewinds_so_todays_document_is_re_read_whole() -> None:
    # The sub-second part is preserved: Slack timestamps are also message ids,
    # and truncating one turns `oldest` into a slightly different message.
    assert _rewind("1700000000.000100") == "1699906400.000100"
    assert _rewind(None) is None
    assert _rewind("not-a-timestamp") is None


def test_a_thread_subject_is_truncated_not_wrapped() -> None:
    assert _subject("") == "thread"
    assert len(_subject("x" * 200)) <= 70
    assert _subject("short question?") == "short question?"


# ─────────────────────────────────────────────────────────────────── notion ──


def _rt(text: str) -> list[dict[str, object]]:
    return [{"plain_text": text, "annotations": {}}]


def test_notion_headings_survive_as_markdown_headings() -> None:
    # The prose chunker splits on headings; losing them means one long slab.
    out = notion_render(
        {"type": "heading_2", "heading_2": {"rich_text": _rt("Auth")}}, 0
    )
    assert out is not None and out.strip() == "## Auth"


def test_nested_list_items_indent_but_headings_do_not() -> None:
    item = notion_render(
        {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt("a")}}, 2
    )
    assert item == "    - a"

    heading = notion_render(
        {"type": "heading_1", "heading_1": {"rich_text": _rt("Top")}}, 2
    )
    assert heading is not None and heading.strip().startswith("# ")


def test_notion_title_is_found_by_property_type_not_by_name() -> None:
    page = {
        "properties": {
            "Some Custom Name": {"type": "title", "title": _rt("Auth decisions")},
            "Owner": {"type": "people", "people": []},
        }
    }
    assert _title(page) == "Auth decisions"
    assert _title({"properties": {}}) == "Untitled"


# ───────────────────────────────────────────────────────────────────── jira ──


def test_jql_window_rewinds_a_minute_and_orders_ascending() -> None:
    jql = _jql("PLAT", "2026-08-17T10:30:45.000+0000", None)
    assert 'project = "PLAT"' in jql
    assert "2026/08/17 10:29" in jql  # one minute of deliberate overlap
    assert jql.endswith("ORDER BY updated ASC")


def test_jql_with_no_filters_is_still_valid() -> None:
    assert _jql(None, None, None) == "ORDER BY updated ASC"


def test_adf_flattens_to_something_a_chunker_can_read() -> None:
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Cause"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Retry budget halved."}],
            },
        ],
    }
    out = _adf(doc)
    assert "## Cause" in out
    assert "Retry budget halved." in out


def test_an_issue_keeps_its_comments() -> None:
    body = _render(
        "PLAT-42",
        {
            "summary": "Checkout failures after v4.2",
            "description": "Spike began at 14:00.",
            "status": {"name": "Done"},
            "issuetype": {"name": "Bug"},
            "comment": {
                "comments": [
                    {
                        "author": {"displayName": "daniel"},
                        "created": "2026-05-02T09:15:00.000+0000",
                        "body": "Reverted the retry change.",
                    }
                ]
            },
        },
    )
    assert "# PLAT-42 · Checkout failures after v4.2" in body
    # The decision is in the comment, not the description — that is the point.
    assert "Reverted the retry change." in body
    assert "daniel" in body
