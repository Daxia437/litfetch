"""Audit: for every entry you intend to cite, is there a file, and is it the right one?

This is the question a reader actually has before submitting: not "how many PDFs do
I own" but "which of my citations rest on a file I have checked". The ledger answers
a neighbouring question (what has been *read*); the audit answers *identity*.

Input is a list of entries, from either source:

    --bibliography review.md      the `**R141.** ...` lines of the project's own review
    --zotero library.json         CSL-JSON exported from Zotero (or BibTeX)

Matching is by (surname, year) in the file name -- the same convention the ledger
uses, and the reason the naming rule exists at all. A match is then **verified**:
the file's title block must agree with the entry's title. A name match is never
taken as proof, because the failure this project keeps finding is precisely a file
named for one paper and containing another.

Verdicts, and they are deliberately not symmetric:

    verified      paired and the title block agrees
    mismatch      paired by name, title block disagrees   <- the valuable row
    unverifiable  paired, but the entry carries no usable title
    missing       entry with no file
    orphan        file with no entry
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import provenance
from .ledger import parse_bibliography
from .store import Library
from .verify import (first_page, longest_run, title_block, title_score,
                     title_tokens, verify_pdf)

# The review writes entries as `Surname, X. (2024). Title. *Venue* ...`, but the
# tail varies: a book puts the title in italics, an arXiv-only entry puts
# `arXiv:` where the venue would be, and one entry writes `(1936–7)`.
# A single regex handled about three quarters of them; the rest are handled by
# the terminator list below, and anything left comes back empty and is reported
# as `unverifiable` rather than guessed.
_YEAR_THEN_TITLE = re.compile(r"\(\d{4}[^)]*\)\.\s+")
_TERMINATORS = (r"\.\s+\*", r"\s+arXiv:", r"\.\s+arXiv", r"\.\s+DOI",
                r"\.\s+https?:", r"\s+（", r"[.。]\s*（", r"\.\s+\[", r"\.\s+\(",
                r"\.\s+[a-z]")   # `. bioRxiv` and friends: a venue, not a title
_ABBREV = re.compile(r"(?:et al|e\.g|i\.e|cf|vs|no|Vol|pp|ed|eds)$", re.I)


@dataclass
class AuditEntry:
    key: str
    title: str = ""
    surname: str = ""
    year: str = ""
    doi: str = ""


@dataclass
class AuditRow:
    key: str
    verdict: str
    file: str = ""
    title: str = ""
    detail: str = ""
    version: str = ""
    score: float = 0.0


def _title_from(body: str) -> str:
    """Pull the title out of one bibliography line, or return "".

    Returning "" is the honest outcome: the audit reports such an entry as
    `unverifiable`, which is a fact about our data, whereas a guess would be a
    fact about our imagination.
    """
    m = _YEAR_THEN_TITLE.search(body)
    if not m:
        return ""
    rest = body[m.end():].strip()
    if not rest or rest.startswith(("arXiv:", "http", "DOI")):
        return ""                                 # an identifier, not a title
    if rest.startswith("*"):                      # the title itself is italicised
        end = rest.find("*", 1)
        return rest[1:end].strip() if end > 0 else ""
    for pat in _TERMINATORS:
        hit = re.search(pat, rest)
        if hit:
            return rest[:hit.start()].strip()
    for hit in re.finditer(r"\.\s+(?=[A-Z])", rest):
        if _ABBREV.search(rest[max(0, hit.start() - 6):hit.start()]):
            continue
        return rest[:hit.start()].strip()
    return rest[:200].strip()


def entries_from_markdown(text: str) -> List[AuditEntry]:
    """Read the review's `**Rnnn.**` lines.

    The title is taken as the text between the year and the next italic or bracket
    marker, which is the shape this review uses throughout. Entries whose title
    cannot be extracted come back with `title=""` and are reported as
    `unverifiable` rather than silently skipped -- a parser that drops what it does
    not understand would flatter the result.
    """
    by_num: Dict[int, AuditEntry] = {}
    for bib in parse_bibliography(text):
        title = _title_from(bib.full or bib.head)
        entry = AuditEntry(key=f"R{bib.num}", title=title,
                           surname=bib.surname, year=bib.year)
        # The review cites a few entries twice (once in the bibliography, once in
        # a table). Keep the copy whose title parsed; a duplicate is not two rows.
        prev = by_num.get(bib.num)
        if prev is None or (not prev.title and entry.title):
            by_num[bib.num] = entry
    return [by_num[k] for k in sorted(by_num)]


def _csl_names(rec: Dict) -> str:
    for key in ("author", "editor"):
        people = rec.get(key) or []
        if people and isinstance(people, list):
            first = people[0]
            if isinstance(first, dict):
                return first.get("family") or first.get("literal") or ""
            return str(first)
    return ""


def entries_from_csl_json(text: str) -> List[AuditEntry]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    out = []
    for i, rec in enumerate(data):
        issued = (rec.get("issued") or {}).get("date-parts") or [[None]]
        year = str((issued[0] or [None])[0] or "")
        out.append(AuditEntry(
            key=str(rec.get("id") or f"item{i}"),
            title=(rec.get("title") or "").strip(),
            surname=_csl_names(rec),
            year=year,
            doi=(rec.get("DOI") or "").strip(),
        ))
    return out


_BIB_FIELD = re.compile(r"(\w+)\s*=\s*\{", re.I)


def entries_from_bibtex(text: str) -> List[AuditEntry]:
    """Minimal BibTeX reader: entry key, title, first author surname, year, doi.

    Deliberately not a full parser. It must not be clever: a wrong field is worse
    than a missing one, and an entry it cannot read becomes `unverifiable`.
    """
    out = []
    for block in re.finditer(r"@(\w+)\s*\{\s*([^,]+),(.*?)\n\}", text, re.S):
        kind, key, body = block.group(1).lower(), block.group(2).strip(), block.group(3)
        if kind in ("comment", "preamble", "string"):
            continue
        fields = {}
        for m in _BIB_FIELD.finditer(body):
            name = m.group(1).lower()
            start = m.end()
            depth, i = 1, start
            while i < len(body) and depth:
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                i += 1
            fields[name] = re.sub(r"\s+", " ", body[start:i - 1]).strip()
        author = fields.get("author", "") or fields.get("editor", "")
        surname = author.split(" and ")[0].split(",")[0].strip() if author else ""
        out.append(AuditEntry(
            key=key,
            title=re.sub(r"[{}]", "", fields.get("title", "")),
            surname=surname,
            year=fields.get("year", ""),
            doi=fields.get("doi", ""),
        ))
    return out


def _fold(text: str) -> str:
    from .ledger import _fold as fold
    return fold(text)


def load_aliases(root: Path) -> tuple:
    """Read `aliases.json` next to the library. Returns (table, problems).

    The audit pairs by (surname, year) because that is the naming rule. The rule
    is not honoured by every file: code-named systems (`VisualPredicator`), a
    Cyrillic surname against a Latin file name, a preprint one year off the
    journal, a file moved to quarantine. Those become `missing` -- a true
    statement about the name, a false one about the library.

    The table is evidence, not convenience, so a row without `why` is refused
    rather than ignored: an unexplained exception is how a ledger rots.
    """
    path = Path(root) / "aliases.json"
    if not path.is_file():
        return {}, []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, [f"aliases.json unreadable: {exc}"]
    table, problems = {}, []
    for key, row in (raw.get("entries") or {}).items():
        if not isinstance(row, dict):
            problems.append(f"{key}: not an object")
            continue
        if not row.get("why"):
            problems.append(f"{key}: no `why` -- refused")
            continue
        if not any(k in row for k in ("file", "duplicate_of", "full_text")):
            problems.append(f"{key}: none of file/duplicate_of/full_text")
            continue
        table[key] = row
    return table, problems


def _pair(entries: List[AuditEntry], files: List[Path]) -> tuple:
    """Pair by (surname, year) in the name; keep unmatched on both sides."""
    by_key: Dict[str, List[Path]] = {}
    for p in files:
        by_key.setdefault(_fold(p.stem), []).append(p)

    pairs, taken = [], set()
    for e in entries:
        surname, year = _fold(e.surname), e.year
        if not surname:
            continue
        hits = [p for stem, paths in by_key.items()
                if surname in stem and (not year or year in stem)
                for p in paths if p not in taken]
        if not hits:
            continue
        if len(hits) > 1 and e.title:
            # Several files claim this entry; keep the one whose title block fits.
            scored = sorted(((title_score(e.title, title_block(p)), p) for p in hits),
                            key=lambda t: t[0], reverse=True)
            best = scored[0][1]
        else:
            best = hits[0]
        taken.add(best)
        pairs.append((e, best))
    return pairs, taken


def _containment(expected: str, block: str) -> float:
    """How much of the title survives when spaces are ignored, as a prefix relation.

    Character n-gram overlap was tried first and is too blunt: the sequel
    "Online Relational Inference for Evolving Multi-agent Interacting Systems"
    shares 0.79 of its 4-grams with "Neural Relational Inference for Interacting
    Systems" -- and it is a different paper. A *prefix* test separates them:

        GEMOT kerned to "GE M OT ... MODELS"  -> block is a prefix of the title
        "Online ..." vs "Neural ..."           -> the 0th character already differs

    Returns 1.0 for containment, otherwise the common-prefix ratio.
    """
    def squash(text: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text.casefold())

    want, have = squash(expected), squash(block)
    if not want or not have:
        return 0.0
    if have in want or want in have:
        return 1.0
    n = 0
    while n < min(len(want), len(have)) and want[n] == have[n]:
        n += 1
    return n / min(len(want), len(have))


def _classify_failure(entry: AuditEntry, path: Path, reason: str) -> tuple:
    """Split "the check failed" into the four things it can actually mean.

    Running the audit on a real library produced 27 `mismatch` rows of which 18
    were the *right paper in another version* and 4 were files with no text layer.
    Collapsing those into "wrong file" is not a small imprecision: it is the same
    mistake this project keeps catching in other people, one level up.
    """
    if ("no extractable text" in reason or "not a PDF" in reason or "only " in reason
        or "almost no text" in reason):
        return ("not-a-pdf" if "not a PDF" in reason else "unverifiable"), reason

    block = title_block(path)
    if not block:
        return "check-failed", reason + " (no title block could be read)"
    score = title_score(entry.title, block)
    exact = longest_run(entry.title, block) >= max(1, len(title_tokens(entry.title)))

    if "different version" in reason:
        # verify_pdf only reaches the year check after the title check passed, so
        # this branch means "right paper, other version" by construction --
        # whatever the title block happens to say.
        return "other-version", (
            "same title, different year in the front matter -- cite the version you hold")
    if exact:
        return "check-failed", f"title matches exactly but the check failed: {reason}"
    if score >= 0.9:
        return "check-failed", f"title words present, layout breaks adjacency: {reason}"
    overlap = _containment(entry.title, block)
    if overlap >= 0.85:
        return "check-failed", (
            f"squashed title agrees at {overlap:.2f} but the line is broken "
            f"(kerning / truncated block); {reason}")
    return "mismatch", f"title agrees only at {score:.2f} (prefix {overlap:.2f}): {reason}"


def audit(library: Library, entries: Iterable[AuditEntry],
          threshold: float = 0.8, limit: Optional[int] = None,
          skip_unverifiable: bool = False,
          min_bytes: int = 5000,
          aliases: Optional[Dict[str, Dict]] = None) -> List[AuditRow]:
    entries = list(entries)
    files = library.pdfs()
    if limit:
        files = files[:limit]
    prov = provenance.load(library.root)
    aliases = aliases or {}

    # Rows the alias table settles before any name matching happens. Two of them
    # are not about files at all: a duplicate entry is a bibliography defect, and
    # a full-text-only entry is a work held as text because the PDF was never
    # obtainable. Both are answers; reporting them as `missing` would hide them.
    rows: List[AuditRow] = []
    settled = set()
    forced: Dict[int, Path] = {}
    for e in entries:
        a = aliases.get(e.key) or {}
        if a.get("duplicate_of"):
            rows.append(AuditRow(e.key, "duplicate", title=e.title,
                                 detail=f"= {a['duplicate_of']} -- {a.get('why', '')}".strip()))
            settled.add(id(e))
        elif a.get("full_text"):
            rel = a["full_text"]
            held = (library.root / rel).is_file()
            rows.append(AuditRow(e.key, "full-text-only" if held else "missing",
                                 file=rel if held else "", title=e.title,
                                 detail=(a.get("why", "") if held
                                         else f"the full text this entry points at is gone: {rel}")))
            settled.add(id(e))
        elif a.get("file"):
            p = library.root / a["file"]
            if p.is_file():
                forced[id(e)] = p
            else:
                rows.append(AuditRow(e.key, "missing", title=e.title,
                                     detail=f"alias points at {a['file']}, which is not there"))
                settled.add(id(e))

    rest = [e for e in entries if id(e) not in settled and id(e) not in forced]
    pairs, _ = _pair(rest, files)
    pairs = pairs + [(e, p) for e in entries if id(e) in forced for p in [forced[id(e)]]]
    paired_keys = {id(e) for e, _ in pairs} | settled

    for entry, path in pairs:
        rel = str(path.relative_to(library.root))
        version = (prov.get(rel) or {}).get("version", "")
        if not entry.title:
            if not skip_unverifiable:
                rows.append(AuditRow(entry.key, "unverifiable", rel, "", 
                                     "entry carries no title to check against", version))
            continue
        verdict = verify_pdf(path, expected_title=entry.title,
                             expected_year=entry.year or None,
                             threshold=threshold, min_bytes=min_bytes)
        if verdict.ok:
            kind, detail = "verified", ""
        else:
            kind, detail = _classify_failure(
                entry, path, "; ".join(verdict.reasons or verdict.warnings))
        rows.append(AuditRow(
            key=entry.key,
            verdict=kind,
            file=rel,
            title=entry.title,
            detail=detail,
            version=version,
            score=round(verdict.coverage, 3),
        ))

    for entry in entries:
        if id(entry) not in paired_keys:
            rows.append(AuditRow(entry.key, "missing", title=entry.title,
                                 detail="no file matches this entry"))

    paired_paths = {p for _, p in pairs}
    for path in files:
        if path not in paired_paths:
            rel = str(path.relative_to(library.root))
            rows.append(AuditRow("", "orphan", rel, "",
                                 "file has no bibliography entry",
                                 (prov.get(rel) or {}).get("version", "")))
    return rows


def summarise(rows: Iterable[AuditRow]) -> Dict[str, int]:
    out = {"verified": 0, "other-version": 0, "mismatch": 0, "check-failed": 0,
           "unverifiable": 0, "not-a-pdf": 0, "duplicate": 0, "full-text-only": 0,
           "missing": 0, "orphan": 0}
    for r in rows:
        out[r.verdict] = out.get(r.verdict, 0) + 1
    return out


def render(rows: List[AuditRow], title: str = "Library audit") -> str:
    counts = summarise(rows)
    total = sum(counts.values())
    lines = [f"# {title}", "",
             f"{total} rows: " + "  ".join(f"**{k}** {v}" for k, v in counts.items()), ""]

    for verdict, heading in (
            ("mismatch", "Wrong file under a right name"),
            ("not-a-pdf", "Not a PDF at all (landing page, stub)"),
            ("check-failed", "Title words present but the check could not complete"),
            ("other-version", "Right paper, other version (cite what you hold)"),
            ("duplicate", "Duplicate entries (same work, two keys)"),
            ("full-text-only", "Held as full text, not as PDF"),
            ("missing", "Entries with no file"),
            ("orphan", "Files with no entry"),
            ("unverifiable", "Could not be checked (no text layer / no title)"),
            ("verified", "Verified")):
        subset = [r for r in rows if r.verdict == verdict]
        if not subset:
            continue
        lines += [f"## {heading} ({len(subset)})", "",
                  "| key | file | version | note |", "|---|---|---|---|"]
        for r in sorted(subset, key=lambda r: r.key):
            lines.append(f"| {r.key or '—'} | `{r.file}` | {r.version or '—'} | "
                         f"{(r.detail or '')[:120]} |")
        lines.append("")
    return "\n".join(lines)
