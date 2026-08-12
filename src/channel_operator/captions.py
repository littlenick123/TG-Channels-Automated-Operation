from __future__ import annotations

import html
import re
import secrets
import unicodedata
from collections.abc import Sequence

from .models import CaptionResult

HASHTAG_RE = re.compile(r"(?<!\w)#[\w]+", re.UNICODE)
INTRO_RE = re.compile(r"^\s*简介\s*[:：]\s*(.*?)\s*$")
TAG_LINE_RE = re.compile(
    r"^\s*(?:标签|【\s*标\s*签\s*】)\s*[:：]\s*(.*?)\s*$"
)


def normalize_tag(tag: str) -> str:
    value = tag.strip()
    if value and not value.startswith("#"):
        value = f"#{value}"
    return unicodedata.normalize("NFKC", value).casefold()


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _truncate_utf16(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    result: list[str] = []
    used = 0
    for character in value:
        units = _utf16_length(character)
        if used + units > limit:
            break
        result.append(character)
        used += units
    return "".join(result).rstrip()


def _truncate_utf16_with_ellipsis(value: str, limit: int) -> str:
    if _utf16_length(value) <= limit:
        return value
    if limit <= 0:
        return ""
    ellipsis = "..."
    ellipsis_units = _utf16_length(ellipsis)
    if limit < ellipsis_units:
        return "." * limit
    prefix = _truncate_utf16(value, limit - ellipsis_units)
    return f"{prefix}{ellipsis}"


def _tag_line_content(line: str) -> str | None:
    labelled = TAG_LINE_RE.match(line)
    if labelled is not None:
        return labelled.group(1)
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return stripped
    return None


def _parse_source(text: str) -> tuple[list[str], str | None]:
    unique: dict[str, str] = {}
    last_line: str | None = None
    last_tag_content: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        tag_content = _tag_line_content(line)
        if tag_content is not None:
            for match in HASHTAG_RE.finditer(tag_content):
                tag = match.group(0)
                unique.setdefault(normalize_tag(tag), tag)
        last_line = line
        last_tag_content = tag_content

    source_intro: str | None = None
    if last_line is not None and last_tag_content is None:
        intro_match = INTRO_RE.match(last_line)
        if intro_match is not None:
            source_intro = intro_match.group(1).strip() or None
        else:
            source_intro = last_line.strip() or None
    return list(unique.values()), source_intro


def build_caption(
    source: str,
    keep_tags: Sequence[str],
    drop_tags: Sequence[str],
    *,
    intro_footer: str = "",
    limit: int = 1024,
    random_source: secrets.SystemRandom | None = None,
) -> CaptionResult:
    rng = random_source or secrets.SystemRandom()
    source_tags, source_intro = _parse_source(source)
    source_by_key = {normalize_tag(tag): tag for tag in source_tags}
    dropped = {normalize_tag(tag) for tag in drop_tags}

    prioritized: list[str] = []
    prioritized_keys: set[str] = set()
    for configured in keep_tags:
        key = normalize_tag(configured)
        if key in source_by_key and key not in dropped and key not in prioritized_keys:
            prioritized.append(source_by_key[key])
            prioritized_keys.add(key)

    remaining = [
        tag
        for tag in source_tags
        if normalize_tag(tag) not in dropped and normalize_tag(tag) not in prioritized_keys
    ]
    count = min(max(0, 5 - len(prioritized)), len(remaining))
    sampled = rng.sample(remaining, count) if count else []
    selected = (prioritized + sampled)[:5]
    fitted: list[str] = []
    for tag in selected:
        candidate = " ".join([*fitted, tag])
        if _utf16_length(candidate) <= limit:
            fitted.append(tag)
    chosen = tuple(fitted)

    tag_line = " ".join(chosen)
    footer = intro_footer.strip()
    separator = "\n" if tag_line and (source_intro or footer) else ""
    available = limit - _utf16_length(tag_line) - _utf16_length(separator)

    intro: str | None = None
    fitted_footer: str | None = None
    if available > 0 and footer:
        if source_intro and available > _utf16_length(footer) + 1:
            intro = _truncate_utf16_with_ellipsis(
                source_intro, available - _utf16_length(footer) - 1
            )
            if intro:
                fitted_footer = footer
        if fitted_footer is None:
            fitted_footer = _truncate_utf16_with_ellipsis(footer, available) or None
    elif available > 0 and source_intro:
        intro = _truncate_utf16_with_ellipsis(source_intro, available) or None

    block_plain = "\n".join(part for part in (intro, fitted_footer) if part)
    plain_parts = [part for part in (tag_line, block_plain) if part]
    plain = "\n".join(plain_parts)
    html_parts: list[str] = []
    if tag_line:
        html_parts.append(f"<b>{html.escape(tag_line)}</b>")
    if block_plain:
        block_html_parts = []
        if intro:
            block_html_parts.append(html.escape(intro))
        if fitted_footer:
            block_html_parts.append(f"<b>{html.escape(fitted_footer)}</b>")
        block_html = "\n".join(block_html_parts)
        html_parts.append(
            f"<blockquote expandable>{block_html}</blockquote>"
        )
    return CaptionResult(
        html="\n".join(html_parts),
        plain=plain,
        tags=chosen,
        intro=block_plain or None,
    )
