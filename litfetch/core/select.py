"""What a `--only` selector means, and whether it selected anything.

A selector is a comma-separated list of terms:

    L007            one reference
    L001-L020       an inclusive numeric range
    COGR-L*         a glob over the reference number

Case-insensitive. A range never eats the hyphen inside an id: `COGR-L07` is a
single id, `L001-L020` is a range, and the difference is decided by whether
both sides of the hyphen end in digits.

Why this exists: a typo in a hand-written list of 65 ids used to be invisible.
The selector kept nothing, the run printed `stored 0`, and exit 1 read exactly
like "there was nothing left to do" -- so a whole retry round went by without
fetching anything. Silence is the bug; `unmatched()` is the fix.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Iterable, List, Sequence, Tuple

# Both sides of the hyphen must be a prefix plus digits, so an id that merely
# contains a hyphen ("COGR-L07") is never mistaken for a range.
_RANGE = re.compile(r"^([A-Za-z]*)(\d+)-([A-Za-z]*)(\d+)$")
_TAIL = re.compile(r"^(\d+)$")


class Selector:
    """The compiled form of a `--only` string. Empty selector keeps everything."""

    def __init__(self, spec: str | None = None):
        self.raw = (spec or "").strip()
        self.terms: List[str] = [t.strip() for t in self.raw.split(",") if t.strip()]
        self.exact: List[str] = []
        self.globs: List[str] = []
        self.ranges: List[Tuple[str, int, int]] = []
        for term in self.terms:
            m = _RANGE.match(term)
            if m and m.group(1).upper() == m.group(3).upper():
                self.ranges.append((m.group(1).upper(), int(m.group(2)), int(m.group(4))))
            elif "*" in term or "?" in term:
                self.globs.append(term.upper())
            else:
                self.exact.append(term.upper())

    def __bool__(self) -> bool:
        return bool(self.terms)

    def match(self, num) -> bool:
        if not self.terms:
            return True
        n = str(num).upper()
        if n in self.exact:
            return True
        for g in self.globs:
            if fnmatch.fnmatchcase(n, g):
                return True
        for prefix, lo, hi in self.ranges:
            if not n.startswith(prefix):
                continue
            tail = _TAIL.match(n[len(prefix):])
            if tail and lo <= int(tail.group(1)) <= hi:
                return True
        return False

    def unmatched(self, nums: Sequence) -> List[str]:
        """Terms that select nothing. Reported, not silently ignored."""
        available = [str(n) for n in nums]
        out = []
        for term in self.terms:
            single = Selector(term)
            if not any(single.match(n) for n in available):
                out.append(term)
        return out


def select(refs: Iterable, spec: str | None):
    """Return (kept, selector, unmatched terms)."""
    sel = Selector(spec)
    refs = list(refs)
    if not sel:
        return refs, sel, []
    kept = [r for r in refs if sel.match(getattr(r, "num", ""))]
    return kept, sel, sel.unmatched([getattr(r, "num", "") for r in refs])


def suggest(term: str, nums: Sequence, limit: int = 3) -> List[str]:
    """Cheap "did you mean" for a term that matched nothing.

    Deliberately not difflib: this package must run on the same Python as the
    user's, and one interpreter in the 2026-11 audit shipped a difflib whose
    `SequenceMatcher` returned 0.0 for identical strings. Prefix containment is
    enough for the mistakes that actually happen (`L01` for `L010`, `l007`),
    plus one numeric rule: `L07` and `L007` are the same number to a human, so
    they must be the same number here even though the strings differ.
    """
    t = str(term).strip().upper()
    if not t:
        return []
    t_prefix, t_number = _parts(t)
    exact_number, containment = [], []
    for n in nums:
        s = str(n).upper()
        if t == s:
            continue
        s_prefix, s_number = _parts(s)
        if (t_prefix is not None and t_prefix == s_prefix
                and t_number is not None and t_number == s_number):
            exact_number.append(str(n))
        elif s.startswith(t) or t.startswith(s) or t in s:
            containment.append(str(n))
    # Same number first, then the closest length: "L01" is likelier a truncation
    # of "L010" than of "L0199".
    containment.sort(key=lambda s: (abs(len(s) - len(t)), s))
    exact_number.sort(key=lambda s: (abs(len(s) - len(t)), s))
    return (exact_number + containment)[:limit]


def _parts(num: str):
    """('L', 7) for 'L007'; (None, None) when the id has no numeric tail."""
    m = re.match(r"^([A-Z]*)(\d+)$", num.upper())
    if not m:
        return None, None
    return m.group(1), int(m.group(2))
