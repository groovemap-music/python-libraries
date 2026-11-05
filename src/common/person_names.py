"""One rule for the surrounding padding of a Person name.

``Person.name`` keys a Neo4j node and ``graph.person.name`` keys the same person in
Postgres. A name that differs only by padding is a second person in both stores, so every
service that derives a person key from a credit name must apply this one function, and the
services must adopt it in the same release: stripping in one store alone splits the keys.
"""

from __future__ import annotations

import unicodedata


# General categories treated as padding: separators (Zs, Zl, Zp), controls (Cc) and format
# characters (Cf, for example U+200C, U+200F and U+FEFF). Cf is not covered by ``str.strip``
# or Cypher ``trim``, which is why padded names survive them.
_PADDING_CATEGORIES = frozenset({"Zs", "Zl", "Zp", "Cc", "Cf"})


def _is_padding(char: str) -> bool:
    return char.isspace() or unicodedata.category(char) in _PADDING_CATEGORIES


def strip_person_name(name: str) -> str:
    """Return ``name`` without padding at its two ends.

    Padding is any Unicode whitespace, control or format character. Interior characters are
    never touched, so ``"A  B"`` and ``"A\u200fB"`` keep their runs and marks. A name that is
    all padding returns ``""``; callers treat that as "no usable name" and skip the credit,
    the same as an empty name. A name with no padding is returned unchanged.
    """
    start = 0
    end = len(name)
    while start < end and _is_padding(name[start]):
        start += 1
    while end > start and _is_padding(name[end - 1]):
        end -= 1
    return name[start:end]
