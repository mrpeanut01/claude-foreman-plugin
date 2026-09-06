"""Glob matching that behaves the same on every supported Python.

pathlib's `**` support moved between 3.12 and 3.13; the loop's protected-path
rules must not depend on which one is installed.

The syntax is GitHub's filter-pattern syntax, because that is what these
patterns are: `branches`, `tags` and `paths` filters read out of workflow files,
plus `protected_paths` from config, which are written to look like them. It is
not fnmatch and not gitignore.
"""

from __future__ import annotations

import re
from functools import lru_cache

# GitHub's cheat sheet limits ranges to a-z, A-Z and 0-9, and a class matches
# exactly one alphanumeric character — so a class can never match `/`, and no
# escapes, negation or POSIX classes need to be understood here.
_CLASS_BODY = re.compile(r"(?:[A-Za-z0-9]-[A-Za-z0-9]|[A-Za-z0-9])+")


def _character_class(pattern: str, start: int) -> tuple[str | None, int]:
    """Compile the bracket expression at `start`, or report it is not one.

    Returns (regex, index just past the closing bracket), or (None, start) when
    the brackets are not a class GitHub would accept: unterminated, holding
    anything but alphanumerics and ranges, or a reversed range like [9-0]. Those
    stay literal text. Inventing a meaning for them — reading `[!x]` as a negated
    class, say — would silently change which branches are filtered and which
    paths are protected.
    """
    end = pattern.find("]", start + 1)
    if end == -1:
        return None, start
    body = pattern[start + 1 : end]
    if not _CLASS_BODY.fullmatch(body):
        return None, start
    try:
        re.compile(f"[{body}]")  # rejects the reversed ranges the body regex allows
    except re.error:
        return None, start
    return f"[{body}]", end + 1


@lru_cache(maxsize=256)
def compile_glob(pattern: str) -> re.Pattern:
    """Compile one GitHub filter pattern into an anchored regex.

    From GitHub's filter-pattern cheat sheet:

        *   zero or more characters, but never `/`
        **  zero or more of any character
        ?   zero or one of the *preceding* character, so `*.jsx?` matches
            page.js as well as page.jsx — a quantifier, not fnmatch's
            match-exactly-one-character
        +   one or more of the preceding character
        []  one alphanumeric character listed in the brackets or covered by a
            range, as in `v[12].[0-9]+.[0-9]+`

    A leading `!` negates the patterns before it, which is a property of the list
    and not of one pattern, so callers handle it (see land._branch_allows).
    """
    # Every wildcard, class and literal character becomes one atom, so a `?` or
    # `+` after it has something to bind to: GitHub's quantifiers apply to the
    # single element in front of them, exactly as a regex quantifier does.
    atoms: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            atoms.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            atoms.append(".*")
            i += 2
            continue
        ch = pattern[i]
        if ch == "*":
            atoms.append("[^/]*")
            i += 1
            continue
        if ch == "[":
            cls, after = _character_class(pattern, i)
            if cls is not None:
                atoms.append(cls)
                i = after
                continue
        if ch in "?+" and atoms:
            # Group the atom before quantifying it: `ab?` must not become `(?:ab)?`,
            # and `*?` must not become `[^/]*?`, which re reads as a lazy
            # quantifier rather than as an optional wildcard.
            atoms[-1] = f"(?:{atoms[-1]}){ch}"
            i += 1
            continue
        # A quantifier with nothing in front of it quantifies nothing, so the
        # only reading left is literal text.
        atoms.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(atoms) + "$")


def matches_any(path: str, patterns) -> str | None:
    """The first pattern that matches, or None."""
    for pattern in patterns or []:
        if compile_glob(pattern).match(path):
            return pattern
    return None
