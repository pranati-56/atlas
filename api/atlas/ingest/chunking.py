"""Type-aware chunking.

Fixed-size character windows cut mid-sentence, mid-table and mid-function, and a
chunk that begins halfway through a clause embeds badly. So the strategy depends
on what the document is:

  prose         headings -> paragraphs -> sentences
  code          file -> symbol (class/function), never splitting a signature
                away from its body unless the body alone exceeds a chunk
  conversation  channel -> thread -> contiguous runs of messages

Every chunk carries a breadcrumb — the path from the document root down to it.
Stored separately from the content so citation snippets stay clean, but folded
into both the embedded text and the tsvector, because "the limit is 100 per
request" is unretrievable without knowing what it is the limit of. That heading
path is this system's cheap version of contextual retrieval: it recovers most of
the benefit of an LLM-generated per-chunk summary at zero marginal cost, which
matters when the corpus is a million chunks.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from atlas.ingest.extract import DocKind, Page

#: Bump when a change here should re-chunk previously ingested documents.
CHUNKER_VERSION = 1

TARGET_CHARS = 1_400
MAX_CHARS = 1_900
#: A trailing fragment shorter than this is folded back into its predecessor.
MIN_CHARS = 220
OVERLAP_RATIO = 0.12


@dataclass(slots=True)
class Chunk:
    ordinal: int
    page: int | None
    #: "Title > Section > Subsection", or "path.py > class X > def y".
    heading: str | None
    content: str
    #: Breadcrumb + content — exactly what gets embedded.
    context_text: str
    token_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    """~4 characters per token holds well enough for English prose to size batches."""
    return max(1, -(-len(text) // 4))


def _breadcrumb(root: str, path: Sequence[str]) -> str:
    """Collapses consecutive repeats.

    A markdown file whose H1 restates its filename is the common case, and
    "Notes > Notes > Fusion" both wastes embedding budget and reads as a bug.
    """
    parts: list[str] = []
    for segment in (root, *path):
        clean = segment.strip()
        if not clean:
            continue
        if parts and parts[-1].lower() == clean.lower():
            continue
        parts.append(clean)
    return " › ".join(parts)


def _make_chunk(
    ordinal: int,
    heading: str | None,
    content: str,
    page: int | None,
    metadata: dict[str, Any] | None = None,
) -> Chunk:
    context_text = f"{heading}\n\n{content}" if heading else content
    return Chunk(
        ordinal=ordinal,
        page=page,
        heading=heading,
        content=content,
        context_text=context_text,
        token_count=estimate_tokens(context_text),
        metadata=metadata or {},
    )


# ─────────────────────────────────────────────────────────────────────  prose ──

_ATX = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
#: "3.2 Fusion" / "IV. Results" — common in PDFs that lost their markup.
_NUMBERED = re.compile(r"^((?:\d+\.){1,3}\d*|[IVX]+\.)\s+(\S.{0,78})$")
#: Sentence-ish split that does not fire on "e.g." or "v1.2".
_SENTENCE = re.compile(r'(?<=[.!?])\s+(?=[A-Z("“\'\d])')


@dataclass(slots=True)
class _Block:
    text: str
    page: int | None
    heading_level: int | None


def _to_blocks(pages: Sequence[Page]) -> list[_Block]:
    blocks: list[_Block] = []
    for p in pages:
        for raw in re.split(r"\n{2,}", p.text):
            block = raw.strip()
            if not block:
                continue

            # A heading is its own block; anything glued to it after a single
            # newline stays with the body.
            lines = block.split("\n")
            first = lines[0].strip()
            atx = _ATX.match(first)
            numbered = _NUMBERED.match(first) if not atx and len(lines) == 1 else None

            if atx:
                blocks.append(_Block(atx.group(2), p.page, len(atx.group(1))))
                rest = "\n".join(lines[1:]).strip()
                if rest:
                    blocks.append(_Block(rest, p.page, None))
            elif numbered:
                # Depth from the number of dotted components: "3.2" is a subsection.
                depth = min(4, numbered.group(1).count(".") + 1)
                blocks.append(_Block(first, p.page, depth))
            else:
                blocks.append(_Block(block, p.page, None))
    return blocks


def _sentences(text: str) -> list[str]:
    parts = _SENTENCE.split(text)
    return parts if parts else [text]


def _split_oversized(text: str) -> list[str]:
    """Hard-splits one oversized block, preferring sentence boundaries."""
    out: list[str] = []
    buf = ""
    for sentence in _sentences(text):
        if len(sentence) > MAX_CHARS:
            # A single sentence longer than a chunk — a table row or a minified
            # blob. Nothing structural left to respect; cut on width.
            if buf:
                out.append(buf.strip())
                buf = ""
            for i in range(0, len(sentence), TARGET_CHARS):
                out.append(sentence[i : i + TARGET_CHARS].strip())
            continue
        if buf and len(buf) + len(sentence) + 1 > TARGET_CHARS:
            out.append(buf.strip())
            buf = ""
        buf = f"{buf} {sentence}" if buf else sentence
    if buf.strip():
        out.append(buf.strip())
    return [p for p in out if p]


def _overlap_tail(text: str) -> str:
    """Tail of a chunk, cut back to a sentence boundary, seeding the next one."""
    want = round(TARGET_CHARS * OVERLAP_RATIO)
    if len(text) <= want:
        return text
    out = ""
    for sentence in reversed(_sentences(text[-want * 2 :])):
        if len(out) >= want:
            break
        out = f"{sentence} {out}".strip() if out else sentence
    return out.strip()


def _chunk_prose(title: str, pages: Sequence[Page]) -> list[Chunk]:
    blocks = _to_blocks(pages)
    chunks: list[Chunk] = []

    heading_path: list[str] = []
    buf = ""
    buf_page: int | None = None
    buf_path: list[str] = []

    def flush() -> None:
        nonlocal buf
        content = buf.strip()
        if not content:
            buf = ""
            return

        previous = chunks[-1] if chunks else None

        # The overlap carried into a buffer is a copy of the previous chunk's
        # tail. If nothing new landed on top of it before the next flush — which
        # happens whenever a section ends right after a split — emitting it
        # would store the same sentences twice and hand the model two identical
        # passages.
        if previous is not None and previous.content.endswith(content):
            buf = ""
            return

        heading = _breadcrumb(title, buf_path)

        # Fold a runt back into its predecessor, but only when they share a
        # section — otherwise the breadcrumb on the merged chunk would be a lie.
        if (
            previous is not None
            and len(content) < MIN_CHARS
            and previous.heading == heading
            and len(previous.content) + len(content) <= MAX_CHARS
        ):
            previous.content = f"{previous.content}\n\n{content}"
            previous.context_text = f"{heading}\n\n{previous.content}"
            previous.token_count = estimate_tokens(previous.context_text)
            buf = ""
            return

        chunks.append(_make_chunk(len(chunks), heading, content, buf_page))
        buf = ""

    for block in blocks:
        if block.heading_level is not None:
            flush()  # a heading closes the section before it
            level = block.heading_level
            heading_path = heading_path[: level - 1]
            while len(heading_path) < level - 1:
                heading_path.append("")
            heading_path.append(block.text)
            heading_path = [h for h in heading_path if h]
            buf_path = list(heading_path)
            buf_page = block.page
            continue

        # A PDF page break also closes the chunk, so no chunk spans two pages
        # and every citation resolves to exactly one page number.
        if buf and buf_page is not None and block.page != buf_page:
            flush()

        if not buf:
            buf_page = block.page
            buf_path = list(heading_path)

        pieces = (
            _split_oversized(block.text)
            if len(block.text) > MAX_CHARS
            else [block.text]
        )
        for piece in pieces:
            if buf and len(buf) + len(piece) + 2 > TARGET_CHARS:
                carry = _overlap_tail(buf)
                flush()
                buf_page = block.page
                buf_path = list(heading_path)
                buf = carry
            buf = f"{buf}\n\n{piece}" if buf else piece

            if len(buf) >= MAX_CHARS:
                carry = _overlap_tail(buf)
                flush()
                buf_page = block.page
                buf_path = list(heading_path)
                buf = carry

    flush()
    for i, c in enumerate(chunks):  # runt-folding leaves gaps
        c.ordinal = i
    return chunks


# ──────────────────────────────────────────────────────────────────────  code ──

# Deliberately regexes over line starts rather than a parser: a real AST needs a
# grammar per language and breaks on the half-written files that show up in any
# repository, whereas a missed boundary here only means a slightly larger chunk.
# Indentation carries the nesting, which is what the breadcrumb needs.
_DEFAULT_SYMBOL = re.compile(
    r"^(\s*)(?:export\s+)?(?:default\s+)?"
    r"(?:public\s+|private\s+|protected\s+|static\s+|abstract\s+|final\s+)*"
    r"(?:async\s+)?"
    r"(?:function|class|interface|type|enum|struct|const|let|var|def|fn|func)\s+"
    r"([A-Za-z_$][\w$]*)"
)

_SYMBOL_PATTERNS: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)"),
    "ruby": re.compile(r"^(\s*)(?:def|class|module)\s+([A-Za-z_][\w:.]*)"),
    "go": re.compile(
        r"^(\s*)func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)|^(\s*)type\s+([A-Za-z_]\w*)"
    ),
    "rust": re.compile(
        r"^(\s*)(?:pub\s+)?(?:async\s+)?"
        r"(?:fn|struct|enum|trait|impl)\s+([A-Za-z_][\w<>]*)"
    ),
    "sql": re.compile(
        r"^(\s*)(?:create|alter)\s+(?:or\s+replace\s+)?"
        r"(?:table|view|function|procedure|index|type)\s+"
        r'(?:if\s+not\s+exists\s+)?([\w."]+)',
        re.IGNORECASE,
    ),
}


@dataclass(slots=True)
class _Symbol:
    name: str
    indent: int
    start: int
    end: int


def _find_symbols(lines: Sequence[str], lang: str | None) -> list[_Symbol]:
    pattern = _SYMBOL_PATTERNS.get(lang or "", _DEFAULT_SYMBOL)
    found: list[_Symbol] = []

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("//", "#")):
            continue
        m = pattern.match(line)
        if not m:
            continue
        # The alternation in the Go pattern shifts the capture groups, so take
        # the first group that matched as the indent and the next as the name.
        groups = [g for g in m.groups() if g is not None]
        if len(groups) < 2 or not groups[1]:
            continue
        found.append(_Symbol(groups[1], len(groups[0]), i, len(lines)))

    # A symbol runs until the next one at the same or shallower indentation.
    for i, current in enumerate(found):
        for nxt in found[i + 1 :]:
            if nxt.indent <= current.indent:
                current.end = nxt.start
                break

    return found


def _chunk_code(
    title: str, pages: Sequence[Page], lang: str | None, path: str | None
) -> list[Chunk]:
    lines = "\n".join(p.text for p in pages).split("\n")
    root = path or title
    symbols = _find_symbols(lines, lang)
    chunks: list[Chunk] = []

    def push(heading: str, body: str, first_line: int) -> None:
        content = body.strip("\n")
        if not content.strip():
            return

        if len(content) > MAX_CHARS:
            # Split an oversized function on blank lines rather than sentences —
            # statement groups are the only structure left inside a body.
            pieces: list[str] = []
            for para in re.split(r"\n{2,}", content):
                if pieces and len(pieces[-1]) + len(para) + 2 <= TARGET_CHARS:
                    pieces[-1] = f"{pieces[-1]}\n\n{para}"
                else:
                    pieces.append(para)
        else:
            pieces = [content]

        symbol = " › ".join(heading.split(" › ")[1:])
        for i, piece in enumerate(pieces):
            meta: dict[str, Any] = {"start_line": first_line + 1}
            if len(pieces) > 1:
                meta["part"] = i + 1
            if symbol:
                meta["symbol"] = symbol
            chunks.append(_make_chunk(len(chunks), heading, piece, None, meta))

    if not symbols:
        # A config file, a script with no declarations, or a language the
        # pattern does not recognise. Fall back to fixed windows over the file.
        i = 0
        while i < len(lines):
            start, size = i, 0
            while i < len(lines) and size < TARGET_CHARS:
                size += len(lines[i]) + 1
                i += 1
            push(_breadcrumb(root, []), "\n".join(lines[start:i]), start)
        for idx, c in enumerate(chunks):
            c.ordinal = idx
        return chunks

    base_indent = symbols[0].indent
    top_level = [s for s in symbols if s.indent == base_indent]

    # Anything before the first symbol — imports, a module docstring, a licence
    # header. Retrievable on its own because "what does this file import" is a
    # real question.
    preamble = "\n".join(lines[: top_level[0].start])
    if len(preamble.strip()) > MIN_CHARS:
        push(_breadcrumb(root, ["imports"]), preamble, 0)

    for sym in top_level:
        body = "\n".join(lines[sym.start : sym.end])
        nested = [
            s for s in symbols if s.indent > sym.indent and sym.start < s.start < sym.end
        ]

        if len(body) <= MAX_CHARS or not nested:
            push(_breadcrumb(root, [sym.name]), body, sym.start)
            continue

        # A large class: emit each method as its own chunk, breadcrumbed under
        # it, so a question about one method does not drag in the whole class.
        cursor = sym.start
        for child in nested:
            if child.start > cursor:
                between = "\n".join(lines[cursor : child.start])
                if len(between.strip()) > MIN_CHARS:
                    push(_breadcrumb(root, [sym.name]), between, cursor)
            child_end = min(child.end, sym.end)
            push(
                _breadcrumb(root, [sym.name, child.name]),
                "\n".join(lines[child.start : child_end]),
                child.start,
            )
            cursor = child_end
        if cursor < sym.end:
            push(
                _breadcrumb(root, [sym.name]),
                "\n".join(lines[cursor : sym.end]),
                cursor,
            )

    for i, c in enumerate(chunks):
        c.ordinal = i
    return chunks


# ──────────────────────────────────────────────────────────────  conversation ──


def _chunk_conversation(title: str, pages: Sequence[Page]) -> list[Chunk]:
    """Transcripts arrive as one page per thread, the first line being a
    "# thread title" marker and each message on its own line.

    A thread is the unit of meaning — a reply is unintelligible without the
    message it answers — so threads stay whole up to the size limit and only
    then split on message boundaries, never mid-message.
    """
    chunks: list[Chunk] = []

    for page in pages:
        lines = page.text.split("\n")
        has_title = bool(lines) and lines[0].startswith("#")
        thread = lines[0].lstrip("# ").strip() if has_title else ""
        messages = [
            line for line in (lines[1:] if has_title else lines) if line.strip()
        ]
        heading = _breadcrumb(title, [thread] if thread else [])

        def flush(pending: list[str], page_no: int | None, thread: str = thread) -> None:
            if not pending:
                return
            meta: dict[str, Any] = {"message_count": len(pending)}
            if thread:
                meta["thread"] = thread
            chunks.append(
                _make_chunk(len(chunks), heading, "\n".join(pending), page_no, meta)
            )

        buf: list[str] = []
        size = 0
        for message in messages:
            if size > 0 and size + len(message) > TARGET_CHARS:
                flush(buf, page.page)
                buf, size = [], 0
            buf.append(message)
            size += len(message) + 1
        flush(buf, page.page)

    for i, c in enumerate(chunks):
        c.ordinal = i
    return chunks


# ─────────────────────────────────────────────────────────────────── dispatch ──


def chunk(
    *,
    kind: DocKind,
    title: str,
    pages: Sequence[Page],
    lang: str | None = None,
    path: str | None = None,
) -> list[Chunk]:
    if kind == "code":
        return _chunk_code(title, pages, lang, path)
    if kind == "conversation":
        return _chunk_conversation(title, pages)
    return _chunk_prose(title, pages)
