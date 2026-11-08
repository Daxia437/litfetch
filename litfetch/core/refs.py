"""The reference list as a file of its own, and the review's bibliography as a view of it.

Why this module exists
----------------------
The numbering used to live in one place only: the 参考文献 section of the review.
But the keys are cited from five other documents (the CA reports, the card, the
roadmap). So the review's bibliography was simultaneously a *paper's bibliography*
and the project's *key registry*, and the two roles pulled it apart -- one work
acquired two keys because two chapters assigned them independently, and nothing
could see the collision until an audit read the whole list.

The fix is structural, not editorial: the registry becomes a file (`REFS.jsonl`),
the review's section becomes a rendered view of it, and `refs check` fails when the
two disagree. A view cannot drift from its source; a hand-copied second list always
does.

Block model
-----------
The section is not a flat list of entries. It has headings, entries, prose
paragraphs, and horizontal rules, and the order carries meaning (batch history).
So the unit is a *block*, kept in order, and `render` reproduces the section byte
for byte. That property is the whole safety argument for adopting this: if the
round trip is exact, nothing is lost by moving.

A block is a dict:

    {"kind": "heading"|"entry"|"prose"|"rule"|"blank", ...}

Entry blocks carry the parsed metadata (`key`, `title`, `year`, `surname`, `doi`)
next to their verbatim `text`, because searching a list by title is the workflow
this file exists to support -- "before downloading, find out whether it is already
there".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .verify import title_score, title_tokens

BIB_LINE = re.compile(r"^\*\*R(\d+)\.\*\*\s*(.*)$")
HEADING = re.compile(r"^\*\*【.*】\*\*$|^\*\*L\d+[^*]*\*\*$|^\*\*L\d+.*\*\*$")

BEGIN = "<!-- BEGIN GENERATED:refs -->"
END = "<!-- END GENERATED:refs -->"

# The review writes `Surname, X. (2024). Title. *Venue*`; the tail varies (a book
# puts the title in italics, an arXiv-only entry puts `arXiv:` where the venue
# would be). Returning "" is the honest failure: `refs check` reports it rather
# than guessing a title into existence.
_YEAR_THEN_TITLE = re.compile(r"\(\d{4}[^)]*\)\.\s+")
_TERMINATORS = (r"\.\s+\*", r"\s+arXiv:", r"\.\s+arXiv", r"\.\s+DOI",
                r"\.\s+https?:", r"\s+（", r"[.。]\s*（", r"\.\s+\[",
                r"\.\s+\(", r"\.\s+[a-z]")
_SURNAME = re.compile(r"^\s*([^\W\d_][\w'\-]+)", re.UNICODE)


@dataclass
class Block:
    kind: str
    text: str = ""
    key: str = ""
    title: str = ""
    year: str = ""
    surname: str = ""
    doi: str = ""
    group: str = ""

    def to_json(self) -> Dict:
        out = {"kind": self.kind}
        if self.kind == "entry":
            out["key"] = self.key
        if self.text:
            out["text"] = self.text
        for name in ("title", "year", "surname", "doi", "group"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        return out

    @classmethod
    def from_json(cls, row: Dict) -> "Block":
        return cls(kind=row.get("kind", "prose"),
                   text=row.get("text", ""),
                   key=row.get("key", ""),
                   title=row.get("title", ""),
                   year=row.get("year", ""),
                   surname=row.get("surname", ""),
                   doi=row.get("doi", ""),
                   group=row.get("group", ""))


def title_of(body: str) -> str:
    """Pull the title out of one bibliography entry, or return ""."""
    m = _YEAR_THEN_TITLE.search(body)
    if not m:
        return ""
    rest = body[m.end():]
    cut = len(rest)
    for pattern in _TERMINATORS:
        hit = re.search(pattern, rest)
        if hit and hit.start() < cut:
            cut = hit.start()
    title = rest[:cut].strip().strip("*").strip()
    return title


def parse_section(text: str, start: str = "## 参考文献",
                  end: str = "## 附录 A") -> List[Block]:
    """Read the bibliography section into ordered blocks.

    Everything between the two headings is preserved, including the blank lines:
    the renderer is required to reproduce the section exactly, and a blank line
    dropped here becomes a diff there.
    """
    if hasattr(text, "read_text"):          # accept a Path as well as its text
        text = Path(text).read_text(encoding="utf-8")
    lines = text.split("\n")
    try:
        i = next(k for k, l in enumerate(lines) if l.startswith(start))
        j = next(k for k, l in enumerate(lines) if l.startswith(end))
    except StopIteration:
        return []

    # Once the section is generated, the prose that introduces it (the numbering
    # note) sits *outside* the markers on purpose: it is a person's explanation,
    # not something the renderer should own or overwrite.
    lo, hi = i + 1, j
    marks = [k for k in range(i + 1, j) if BEGIN in lines[k] or END in lines[k]]
    if len(marks) >= 2:
        lo, hi = marks[0] + 1, marks[-1]
    body = lines[lo:hi]

    blocks: List[Block] = []
    group = ""
    for line in body:
        if BEGIN in line or END in line:
            continue
        m = BIB_LINE.match(line)
        if m:
            key, rest = f"R{m.group(1)}", m.group(2)
            ym = re.search(r"\((\d{4})", rest)
            sm = _SURNAME.match(rest)
            dm = re.search(r"10\.\d{4,9}/[^\s）]+", rest)
            blocks.append(Block(kind="entry", key=key, text=rest,
                                title=title_of(rest),
                                year=ym.group(1) if ym else "",
                                surname=_fold(sm.group(1)) if sm else "",
                                doi=dm.group(0) if dm else "",
                                group=group))
            continue
        if line.strip() == "---":
            blocks.append(Block(kind="rule", text=line))
            continue
        if HEADING.match(line.strip()):
            group = line.strip().strip("*【】")
            blocks.append(Block(kind="heading", text=line, group=group))
            continue
        blocks.append(Block(kind="blank" if not line.strip() else "prose",
                            text=line, group=group))
    return blocks


def render(blocks: Iterable[Block]) -> str:
    """Reproduce the section exactly. `refs check` compares this with the review."""
    out = []
    for b in blocks:
        if b.kind == "entry":
            out.append(f"**{b.key}.** {b.text}")
        else:
            out.append(b.text)
    return "\n".join(out)


def load(path: str | Path) -> List[Block]:
    p = Path(path)
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(Block.from_json(json.loads(line)))
    return rows


def save(path: str | Path, blocks: Iterable[Block]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for b in blocks:
            fh.write(json.dumps(b.to_json(), ensure_ascii=False) + "\n")


def _fold(text: str) -> str:
    from .ledger import _fold as fold
    return fold(text)


@dataclass
class Match:
    key: str
    score: float
    title: str
    year: str
    reason: str


def find(blocks: Iterable[Block], query: str, year: str = "",
         surname: str = "", threshold: float = 0.72) -> List[Match]:
    """Is this paper already in the list?

    Called *before* downloading, which is the whole point: the cheapest duplicate
    is the one never fetched. Three signals, strongest first -- exact DOI, title
    score, and (surname, year) -- because a title alone misses a preprint whose
    wording moved, and a surname alone is a class, not a paper.
    """
    q = query.strip()
    q_doi = re.search(r"10\.\d{4,9}/[^\s）]+", q)
    q_title = title_of(q) or q
    out: List[Match] = []
    for b in blocks:
        if b.kind != "entry":
            continue
        reasons, score = [], 0.0
        if q_doi and b.doi and q_doi.group(0).lower() == b.doi.lower():
            reasons.append("same DOI")
            score = 1.0
        if b.title and q_title:
            s = title_score(q_title, b.title)
            if s >= threshold:
                reasons.append(f"title {s:.2f}")
                score = max(score, s)
        if surname and year and _fold(surname) == b.surname and year == b.year:
            reasons.append("same surname+year")
            score = max(score, 0.75)
        if reasons:
            out.append(Match(b.key, round(score, 2), b.title, b.year, ", ".join(reasons)))
    return sorted(out, key=lambda m: -m.score)


def apply_render(text, blocks: Iterable[Block]) -> str:
    """Replace only the generated region, leaving the prose around it alone."""
    if hasattr(text, "read_text"):
        text = Path(text).read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise ValueError("the review has no BEGIN/END GENERATED:refs markers")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{BEGIN}\n{render(blocks)}\n{END}{tail}"


def check(review_text, blocks: Iterable[Block]) -> List[str]:
    """Does the review's section still equal the list? Returns problems."""
    problems = []
    if hasattr(review_text, "read_text"):
        review_text = Path(review_text).read_text(encoding="utf-8")
    blocks = list(blocks)
    if BEGIN not in review_text or END not in review_text:
        problems.append("review has no GENERATED:refs markers -- cannot verify it against the list")
        return problems
    inside = review_text.split(BEGIN, 1)[1].split(END, 1)[0].strip("\n")
    expected = render(blocks)
    if inside != expected:
        got, want = inside.split("\n"), expected.split("\n")
        first = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b),
                     min(len(got), len(want)))
        problems.append(
            f"review section differs from REFS.jsonl at line {first + 1}: "
            f"review={got[first][:70] if first < len(got) else '(end)'!r} "
            f"list={want[first][:70] if first < len(want) else '(end)'!r}")
    keys = [b.key for b in blocks if b.kind == "entry"]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        problems.append(f"duplicate keys in the list: {', '.join(dup)}")
    empty = [b.key for b in blocks if b.kind == "entry" and not b.title]
    if empty:
        problems.append(f"entries whose title could not be parsed: {', '.join(empty)}")
    return problems
