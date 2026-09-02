"""Parsing: bytes or text in, normalised pages out.

Everything downstream works on `list[Page]`, so a connector only has to say what
kind of thing it fetched. Page numbers are meaningful for PDFs and None for
everything else, which is what lets a citation resolve to "p.14" only when that
is a real claim.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Literal

#: Bump when a parser change should invalidate previously ingested documents.
PARSER_VERSION = 1

DocKind = Literal["pdf", "docx", "md", "html", "txt", "code", "conversation"]


class ExtractionError(Exception):
    """A problem with the document, not with Atlas. Surfaced to the user as-is."""


@dataclass(slots=True)
class Page:
    page: int | None
    text: str


@dataclass(slots=True)
class Extraction:
    pages: list[Page]
    parser_version: int = PARSER_VERSION
    #: Detected language for code, so the chunker can pick a symbol grammar.
    lang: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


_EXT_TO_KIND: dict[str, DocKind] = {
    "pdf": "pdf",
    "docx": "docx",
    "md": "md",
    "markdown": "md",
    "mdx": "md",
    "html": "html",
    "htm": "html",
    "txt": "txt",
    "text": "txt",
    "rst": "txt",
    "csv": "txt",
    **dict.fromkeys(
        (
            "json yaml yml toml ts tsx js jsx mjs cjs py rb go rs java kt swift "
            "c h cc cpp hpp cs php sh bash sql"
        ).split(),
        "code",
    ),
}

_EXT_TO_LANG = {
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "py": "python",
    "rb": "ruby",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "kt": "kotlin",
    "swift": "swift",
    "c": "c",
    "h": "c",
    "cc": "cpp",
    "cpp": "cpp",
    "hpp": "cpp",
    "cs": "csharp",
    "php": "php",
    "sh": "shell",
    "bash": "shell",
    "sql": "sql",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
}


def kind_from_path(path: str) -> DocKind | None:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _EXT_TO_KIND.get(ext)


def lang_from_path(path: str) -> str | None:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _EXT_TO_LANG.get(ext)


def _tidy(text: str) -> str:
    """Collapses the whitespace PDF and HTML extraction leaves behind."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"\n{3,}", "\n\n", text)  # 3+ blank lines carry no structure
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    return text.strip()


_ENTITIES = {
    "&nbsp;": " ",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
}


def strip_html(html: str) -> str:
    out = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    out = re.sub(r"<style[\s\S]*?</style>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"<!--[\s\S]*?-->", "", out)
    # Turn structural tags into the blank lines the chunker splits on, and
    # headings into ATX markers, before discarding the rest.
    out = re.sub(
        r"<h([1-6])[^>]*>",
        lambda m: "\n\n" + "#" * int(m.group(1)) + " ",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"</(p|div|section|article|li|tr|h[1-6])>", "\n\n", out, flags=re.IGNORECASE
    )
    out = re.sub(r"<br\s*/?>", "\n", out, flags=re.IGNORECASE)
    out = re.sub(r"<[^>]+>", "", out)
    for entity, char in _ENTITIES.items():
        out = out.replace(entity, char)
    out = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), out)
    # Ampersand last, so "&amp;lt;" does not become "<".
    out = out.replace("&amp;", "&")
    return _tidy(out)


def _extract_pdf(data: bytes) -> list[Page]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [
        Page(page=i + 1, text=_tidy(p.extract_text() or ""))
        for i, p in enumerate(reader.pages)
    ]
    pages = [p for p in pages if p.text]

    if not pages:
        raise ExtractionError(
            "This PDF has no text layer — it is probably a scan. Run OCR over it "
            "first; indexing it as-is would store an empty document."
        )
    return pages


def _extract_docx(data: bytes) -> list[Page]:
    import docx  # python-docx

    document = docx.Document(io.BytesIO(data))
    lines: list[str] = []
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        # Preserve heading levels as ATX so the prose chunker can build a
        # breadcrumb; raw text extraction would throw the structure away.
        style = (para.style.name or "") if para.style else ""
        match = re.match(r"Heading (\d)", style)
        lines.append(f"{'#' * int(match.group(1))} {text}" if match else text)

    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))

    text = _tidy("\n\n".join(lines))
    if not text:
        raise ExtractionError("The .docx contained no readable text.")
    return [Page(page=None, text=text)]


def _decode(data: bytes | None) -> str:
    if data is None:
        raise ExtractionError("Nothing to extract — no bytes and no text.")
    return data.decode("utf-8", errors="replace")


def extract(
    *,
    kind: DocKind,
    data: bytes | None = None,
    text: str | None = None,
    path: str | None = None,
) -> Extraction:
    if kind == "pdf":
        if data is None:
            raise ExtractionError("PDF extraction needs bytes.")
        return Extraction(pages=_extract_pdf(data))

    if kind == "docx":
        if data is None:
            raise ExtractionError("DOCX extraction needs bytes.")
        return Extraction(pages=_extract_docx(data))

    if kind == "html":
        body = strip_html(text if text is not None else _decode(data))
        if not body:
            raise ExtractionError("The HTML contained no readable text.")
        return Extraction(pages=[Page(page=None, text=body)])

    if kind == "code":
        body = text if text is not None else _decode(data)
        if not body.strip():
            raise ExtractionError("The file is empty.")
        # Never tidied: indentation is semantic in code, and collapsing blank
        # lines would merge functions the symbol chunker splits on.
        return Extraction(
            pages=[
                Page(page=None, text=body.replace("\r\n", "\n").replace("\r", "\n"))
            ],
            lang=lang_from_path(path) if path else None,
        )

    body = _tidy(text if text is not None else _decode(data))
    if not body:
        raise ExtractionError("The file is empty.")
    return Extraction(pages=[Page(page=None, text=body)])
