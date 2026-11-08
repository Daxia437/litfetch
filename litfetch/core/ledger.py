"""Reading-status bookkeeping.

The question this answers is not "how many PDFs do we have" but "how many of
them have been read, and where is the evidence". The rules are deliberately
mechanical, because the interesting cases are exactly the ones a human forgets:

    A  the file name carries (surname, year) of a bibliography entry  -> full read
    B  the caller declares it (e.g. an "abstracts only" list)          -> skimmed
    C  neither                                                        -> never read
    X  not a PDF, not literature, or a known wrong link                -> unusable

`evidence` is whatever the caller wants to point at. Nothing here reads the
review's prose: the bibliography is the contract.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .store import MISFILED, Library
from .verify import sha256_of, sniffs

BIB_LINE = re.compile(r"\*\*R(\d+)\.\*\*\s*([^\n]+)")


@dataclass
class BibEntry:
    num: int
    surname: str
    year: str
    head: str          # first 70 characters: enough to quote in a message
    full: str = ""     # the whole line: the audit needs the title out of it


@dataclass
class Row:
    path: Path
    status: str
    evidence: str = ""
    duplicate_of: Optional[Path] = None


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def parse_bibliography(text: str) -> List[BibEntry]:
    out = []
    for num, body in BIB_LINE.findall(text):
        # \w keeps accented letters (Schölkopf, Kőszegi, Gödel). A plain
        # [A-Za-z] class silently truncates them to "Sch" and the match then fails.
        m = re.match(r"\s*([^\W\d_][\w'\-]+)", body, re.UNICODE)
        y = re.search(r"\((\d{4})", body)
        if not m or not y:
            continue
        out.append(BibEntry(int(num), _fold(m.group(1)), y.group(1), body[:70], body))
    return out


def classify(
    library: str | Path,
    bib: Iterable[BibEntry] = (),
    skimmed: Iterable[str] = (),
    overrides: Optional[Dict[str, str]] = None,
    non_target: Iterable[str] = ("logo",),
) -> List[Row]:
    """Classify every PDF. `overrides` maps a *file-name fragment* (casefold) to
    the evidence string, for papers the bibliography cannot match mechanically:
    the review cites the German original, the merged volume, a renamed preprint,
    or a paper by title only. Keep the override table next to the bibliography --
    it is part of the evidence, not a convenience."""
    lib = Library(library)
    bib = list(bib)
    skim = {s.casefold() for s in skimmed}
    seen: Dict[str, Path] = {}
    rows: List[Row] = []
    overrides = {k.casefold(): v for k, v in (overrides or {}).items()}
    skip = [k.casefold() for k in non_target]

    for p in lib.pdfs():
        rel = p.relative_to(lib.root)
        digest = sha256_of(p)
        dup = seen.get(digest)          # relative, so it can be compared with Row.path
        seen.setdefault(digest, rel)

        status, evidence = "C", ""
        relfold = _fold(str(rel))
        if sniffs(p) != "pdf":
            status, evidence = "X", "not a PDF"
        elif MISFILED in p.parts:
            status, evidence = "X", "quarantined"
        elif any(k in relfold for k in skip):
            status, evidence = "X", "not target literature"
        else:
            name = _fold(p.name)
            hits = [e for e in bib if e.surname in name and e.year in name]
            if hits:
                status, evidence = "A", f"R{hits[0].num}"
            elif any(k in relfold for k in overrides):
                key = next(k for k in overrides if k in relfold)
                status, evidence = "A", overrides[key]
            elif any(k in relfold for k in skim):
                status, evidence = "B", "declared skim"
        rows.append(Row(path=rel, status=status, evidence=evidence, duplicate_of=dup))
    return rows


def summary(rows: Iterable[Row]) -> dict:
    rows = list(rows)
    files = len(rows)
    uniq = len({r.duplicate_of or r.path for r in rows})
    counts = Counter(r.status for r in rows)
    return {
        "files": files,
        "distinct": uniq,
        "duplicate_copies": files - uniq,
        **{k.lower(): counts.get(k, 0) for k in ("A", "B", "C", "X")},
        "unread": [str(r.path) for r in rows if r.status == "C"],
    }


def render(rows: Iterable[Row], heading: str = "Reading status (auto-generated)") -> str:
    rows = list(rows)
    s = summary(rows)
    lines = [f"### {heading}", "",
             f"- files **{s['files']}**, distinct **{s['distinct']}**, "
             f"duplicate copies {s['duplicate_copies']}",
             f"- A {s['a']} · B {s['b']} · C {s['c']} · X {s['x']}", ""]
    if s["unread"]:
        lines += ["Unread (C):", ""] + [f"- `{u}`" for u in s["unread"]] + [""]
    by_dir: Dict[str, Counter] = {}
    for r in rows:
        top = str(r.path).split("/")[0]
        by_dir.setdefault(top, Counter())[r.status] += 1
    lines += ["| layer | A | B | C | X |", "|---|---|---|---|---|"]
    for top in sorted(by_dir):
        c = by_dir[top]
        lines.append(f"| {top} | {c.get('A',0)} | {c.get('B',0)} | {c.get('C',0)} | {c.get('X',0)} |")
    return "\n".join(lines) + "\n"


BEGIN = "<!-- BEGIN GENERATED:reading-status -->"
END = "<!-- END GENERATED:reading-status -->"


def render_block(rows: Iterable[Row], heading: str = "Counts") -> str:
    """The machine-owned region of a reading-status file.

    Prose (why the ledger exists, what the integrity findings were) stays outside
    the markers and is written by a human; everything that must never drift --
    counts, per-layer tallies, the file table -- lives inside.
    """
    rows = list(rows)
    s = summary(rows)
    by_dir: Dict[str, Counter] = {}
    for r in rows:
        by_dir.setdefault(str(r.path).split("/")[0], Counter())[r.status] += 1

    out = [BEGIN,
           f"**{heading}** (generated; run `litfetch ledger --write <file>` to refresh)",
           "",
           "| quantity | value |", "|---|---|",
           f"| PDF files on disk | **{s['files']}** |",
           f"| distinct documents (sha256) | **{s['distinct']}** |",
           f"| duplicate copies (same hash) | {s['duplicate_copies']} |",
           f"| **A** full read, evidence on record | {s['a']} files |",
           f"| **B** abstract + conclusion skim only | {s['b']} files |",
           f"| **C** downloaded, never read | **{s['c']}** |",
           f"| **X** unusable / not target literature | {s['x']} files |",
           "",
           "| layer | A | B | C | X |", "|---|---|---|---|---|"]
    for top in sorted(by_dir):
        c = by_dir[top]
        out.append(f"| {top} | {c.get('A',0)} | {c.get('B',0)} | {c.get('C',0)} | {c.get('X',0)} |")

    if s["unread"]:
        out += ["", "**Unread (C)**", ""] + [f"- `{u}`" for u in s["unread"]]

    out += ["", "## Full ledger"]
    for top in sorted(by_dir):
        out += ["", f"### {top}", "", "| file | status | evidence |", "|---|---|---|"]
        for r in sorted(by_dir_rows(rows, top), key=lambda x: str(x.path)):
            name = str(r.path).split("/", 1)[1] if "/" in str(r.path) else str(r.path)
            note = r.evidence
            if r.duplicate_of:
                note += f" · duplicate of `{Path(str(r.duplicate_of)).name}`"
            out.append(f"| `{name}` | {r.status} | {note} |")
    out.append(END)
    return "\n".join(out) + "\n"


def by_dir_rows(rows: Iterable[Row], top: str) -> List[Row]:
    return [r for r in rows if str(r.path).split("/")[0] == top]


def write_into(path: str | Path, block: str) -> str:
    """Replace the generated region in `path`; append it when no markers exist."""
    p = Path(path)
    if p.exists():
        text = p.read_text(encoding="utf-8")
        if BEGIN in text and END in text:
            head = text[: text.index(BEGIN)]
            tail = text[text.index(END) + len(END):]
            merged = head + block + tail
            p.write_text(merged, encoding="utf-8")
            return "replaced"
        p.write_text(text.rstrip() + "\n\n" + block, encoding="utf-8")
        return "appended"
    p.write_text(block, encoding="utf-8")
    return "created"


def to_json(rows: Iterable[Row]) -> str:
    return json.dumps([{"path": str(r.path), "status": r.status,
                        "evidence": r.evidence,
                        "duplicate_of": str(r.duplicate_of) if r.duplicate_of else None}
                       for r in rows], ensure_ascii=False, indent=1)
