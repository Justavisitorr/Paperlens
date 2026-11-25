"""Preserve native contact-only metadata when a model rewrites an address."""

from __future__ import annotations

import re
from collections import Counter

_EMAIL = re.compile(
    r"(?:\{[^{}@\n]+\}|[A-Za-z0-9_.+%'\-]+)@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
_CONTACT_LABEL = re.compile(r"\b(?:e-?mails?|contact)\b\s*[:：]?|电子邮箱\s*[:：]?", re.I)


def native_contact_content(content: str, source_texts: list[str]) -> str:
    """Use exact native contact-only blocks; leave names and affiliations alone.

    The caller supplies only actual same-page source IDs owned by an authors
    information row. Mixed prose/contact blocks are deliberately not rewritten.
    """
    native = []
    for text in source_texts:
        if _EMAIL.search(text) and not _CONTACT_LABEL.sub("", _EMAIL.sub("", text)).strip(
            " \t\r\n,;；，:：()（）<>"
        ):
            native.append(text.strip())
    if not native:
        return content
    expected = Counter(_EMAIL.findall("\n".join(native)))
    if all(text in content for text in native) and Counter(_EMAIL.findall(content)) == expected:
        return content
    # Model addresses are not authoritative when exact contact source blocks
    # are available. Keep the other metadata and append the original blocks.
    other = []
    for line in _EMAIL.sub("", content).splitlines():
        line = line.strip(" \t,;；，")
        if line and _CONTACT_LABEL.sub("", line).strip(" \t:：,;；，()（）<>"):
            other.append(line)
    return "\n".join([*other, *native])
