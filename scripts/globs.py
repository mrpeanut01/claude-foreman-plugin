"""Glob matching that behaves the same on every supported Python.

pathlib's `**` support moved between 3.12 and 3.13; the loop's protected-path
rules must not depend on which one is installed.
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=256)
def compile_glob(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        ch = pattern[i]
        out.append("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def matches_any(path: str, patterns) -> str | None:
    """The first pattern that matches, or None."""
    for pattern in patterns or []:
        if compile_glob(pattern).match(path):
            return pattern
    return None
