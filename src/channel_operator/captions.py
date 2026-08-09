from __future__ import annotations

import html
import re
import secrets
import unicodedata
from collections.abc import Sequence

from .models import CaptionResult

HASHTAG_RE = re.compile(r"(?<!\w)#[\w]+", re.UNICODE)
INTRO_RE = re.compile(r"^\s*简介\s*[:：]\s*(.*?)\s*$")
TAG_LINE_RE = re.compile(r"^\s*标签\s*[:：]\s*(.*?)\s*$")


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


def _source_tags(text: str) -> list[str]:
    unique: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.lstrip()
        labelled = TAG_LINE_RE.match(line)
        if not stripped.startswith("#") and labelled is None:
            continue
        content = labelled.group(1) if labelled is not None else stripped
        for match in HASHTAG_RE.finditer(content):
            tag = match.group(0)
            unique.setdefault(normalize_tag(tag), tag)
    return list(unique.values())


def _intro(text: str) -> str | None:
    for line in text.splitlines():
        match = INTRO_RE.match(line)
        if match:
            value = match.group(1).strip()
            return value or None
    return None


def build_caption(
    source: str,
    keep_tags: Sequence[str],
    drop_tags: Sequence[str],
    *,
    limit: int = 1024,
    random_source: secrets.SystemRandom | None = None,
) -> CaptionResult:
    rng = random_source or secrets.SystemRandom()
    source_tags = _source_tags(source)
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
    intro = _intro(source)
    if intro is not None:
        separator = "\n\n" if tag_line else ""
        available = limit - _utf16_length(tag_line) - _utf16_length(separator)
        intro = _truncate_utf16_with_ellipsis(intro, available)
        if not intro:
            intro = None

    plain_parts = [part for part in (tag_line, intro) if part]
    plain = "\n\n".join(plain_parts)
    html_parts: list[str] = []
    if tag_line:
        html_parts.append(html.escape(tag_line))
    if intro:
        html_parts.append(f"<blockquote>{html.escape(intro)}</blockquote>")
    return CaptionResult(html="\n\n".join(html_parts), plain=plain, tags=chosen, intro=intro)
