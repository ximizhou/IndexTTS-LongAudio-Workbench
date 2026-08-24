"""Text loading, normalization, segmentation, and integrity checks.

The splitter deliberately treats text as an ordered stream of Unicode code
points.  It may choose a natural sentence boundary, but it never drops or
reorders characters; ``''.join(segment.text for segment in segments)`` is
always the normalized source text.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class TextSegment:
    """A stable, serializable text segment."""

    id: str
    index: int
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["char_count"] = self.char_count
        return value


_MARKDOWN_FENCE = re.compile(r"^\s*(```+|~~~+).*$", re.MULTILINE)
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MARKDOWN_QUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_MARKDOWN_BULLET = re.compile(r"^\s{0,3}[-+*]\s+", re.MULTILINE)
_MARKDOWN_ORDERED = re.compile(r"^\s{0,3}\d+[.)]\s+", re.MULTILINE)
_MARKDOWN_LINK = re.compile(r"!?(\[([^\]]*)\])\([^)]*\)")
_MARKDOWN_REFERENCE = re.compile(r"!?(\[([^\]]*)\])\s*\[[^\]]*\]")
_MARKDOWN_CODE = re.compile(r"`([^`]+)`")
_MARKDOWN_STRONG = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_MARKDOWN_EMPHASIS = re.compile(r"(?<!\w)(\*|_)([^*_\n]+)\1")
_HTML_TAG = re.compile(r"<[^>]+>")
_HORIZONTAL_SPACE = re.compile(r"[\t\f\v ]+")
_EXCESS_NEWLINES = re.compile(r"\n{3,}")

_PUNCTUATION_MAP = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "′": "'",
        "＂": '"',
        "＇": "'",
        "　": " ",
        "…": "...",
        "—": "-",
        "–": "-",
        "−": "-",
        "‐": "-",
        "‑": "-",
        "﹣": "-",
        "～": "~",
        "＆": "&",
        "％": "%",
        "＋": "+",
        "＝": "=",
        "＃": "#",
        "＠": "@",
    }
)


def read_text_file(path: str | Path) -> str:
    """Read a user text file with BOM-aware UTF and Chinese fallbacks.

    UTF-8 is preferred.  ``gb18030`` is deliberately included because many
    Chinese ``.txt`` files are saved using that Windows encoding.  A decode
    error is surfaced rather than silently replacing user characters.
    """

    raw = Path(path).read_bytes()
    # BOMs are unambiguous and should win over the generic candidate list.
    for encoding, bom in (
        ("utf-8-sig", b"\xef\xbb\xbf"),
        ("utf-32", b"\xff\xfe\x00\x00"),
        ("utf-32", b"\x00\x00\xfe\xff"),
        ("utf-16", b"\xff\xfe"),
        ("utf-16", b"\xfe\xff"),
    ):
        if raw.startswith(bom):
            return raw.decode(encoding)
    errors: list[str] = []
    for encoding in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    raise UnicodeError("无法识别文本编码；尝试了 UTF-8、GB18030、Big5: " + "; ".join(errors))


def normalize_text(
    text: str,
    *,
    terms: Mapping[str, str] | None = None,
    strip_markdown: bool = True,
) -> str:
    """Normalize text for speech while retaining all visible content.

    Markdown decoration is removed, link labels are retained, common Unicode
    compatibility forms and typography are normalized, and whitespace is made
    deterministic.  ``terms`` is an optional exact replacement mapping for
    project-specific pronunciation aliases; keys are applied longest-first.
    """

    if not isinstance(text, str):
        raise TypeError("text must be str")
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\ufeff", "")
    if strip_markdown:
        # Fences are decoration.  Keep their contents, but remove fence lines.
        value = _MARKDOWN_FENCE.sub("", value)
        value = _MARKDOWN_LINK.sub(lambda m: m.group(2), value)
        value = _MARKDOWN_REFERENCE.sub(lambda m: m.group(2), value)
        value = _MARKDOWN_CODE.sub(lambda m: m.group(1), value)
        value = _MARKDOWN_STRONG.sub(lambda m: m.group(2), value)
        value = _MARKDOWN_EMPHASIS.sub(lambda m: m.group(2), value)
        value = _MARKDOWN_HEADING.sub("", value)
        value = _MARKDOWN_QUOTE.sub("", value)
        value = _MARKDOWN_BULLET.sub("", value)
        value = _MARKDOWN_ORDERED.sub("", value)
        value = _HTML_TAG.sub("", value)
        value = html.unescape(value)
    value = unicodedata.normalize("NFKC", value).translate(_PUNCTUATION_MAP)
    # Normalize horizontal spacing per line but preserve paragraph boundaries.
    value = "\n".join(_HORIZONTAL_SPACE.sub(" ", line).strip() for line in value.split("\n"))
    value = _EXCESS_NEWLINES.sub("\n\n", value)
    if terms:
        for source in sorted(terms, key=len, reverse=True):
            if source:
                value = value.replace(source, terms[source])
    return value.strip()


_BREAK_CHARS = frozenset("。！？!?；;:\n")
_SOFT_BREAK_CHARS = frozenset("，,、")


def _natural_cut(text: str, start: int, limit: int) -> int:
    """Return a cut index in ``(start, start + limit]`` when possible."""

    end = min(len(text), start + limit)
    if end >= len(text):
        return end
    # Prefer the last hard sentence/paragraph boundary.  A boundary character
    # stays in the preceding segment, preserving the exact source stream.
    for index in range(end, start, -1):
        if text[index - 1] in _BREAK_CHARS:
            return index
    for index in range(end, start, -1):
        if text[index - 1] in _SOFT_BREAK_CHARS:
            return index
    # Whitespace is a useful fallback for Latin prose.  It remains part of
    # the preceding segment, so integrity is unchanged.
    for index in range(end, start, -1):
        if text[index - 1].isspace():
            return index
    return end


def split_text(text: str, max_chars: int = 140) -> list[TextSegment]:
    """Split normalized text into ordered segments without dropping content."""

    if not isinstance(text, str):
        raise TypeError("text must be str")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    if not text:
        return []
    segments: list[TextSegment] = []
    start = 0
    while start < len(text):
        end = _natural_cut(text, start, max_chars)
        if end <= start:  # Defensive guard for unexpected Unicode behavior.
            end = min(len(text), start + max_chars)
        segment_text = text[start:end]
        segments.append(TextSegment(id=f"seg-{len(segments):04d}", index=len(segments), text=segment_text))
        start = end
    return segments


def join_segments(segments: Iterable[TextSegment | Mapping[str, object]]) -> str:
    """Join segments in supplied order, rejecting duplicate or missing indices."""

    normalized: list[tuple[int, str]] = []
    for segment in segments:
        if isinstance(segment, TextSegment) or (hasattr(segment, "index") and hasattr(segment, "text")):
            normalized.append((int(segment.index), str(segment.text)))
        else:
            normalized.append((int(segment["index"]), str(segment["text"])))
    if [index for index, _ in normalized] != list(range(len(normalized))):
        raise ValueError("segments must contain contiguous indices starting at zero")
    return "".join(text for _, text in normalized)


def validate_integrity(original: str, segments: Sequence[TextSegment | Mapping[str, object]]) -> bool:
    """Return whether segment order and concatenated text exactly match source."""

    try:
        indices = [
            int(item.index) if (isinstance(item, TextSegment) or hasattr(item, "index")) else int(item["index"])
            for item in segments
        ]
        if indices != list(range(len(indices))):
            return False
        # ``join_segments`` sorts defensively for callers reading a manifest;
        # the explicit index check above ensures validation rejects a reordered
        # sequence supplied by an API or test.
        return join_segments(segments) == original
    except (KeyError, TypeError, ValueError):
        return False


__all__ = [
    "TextSegment",
    "join_segments",
    "normalize_text",
    "read_text_file",
    "split_text",
    "validate_integrity",
]
