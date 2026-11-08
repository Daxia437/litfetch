"""Library layout: naming, de-duplication, quarantine.

Conventions this module enforces because they were violated by hand once each:

* a file name is `Author_Year_ShortTitle.pdf`, sanitised so that it is legal on
  Windows as well as POSIX (no `:` `?` `*` `"` `<` `>` `|`, no trailing dot,
  not a reserved DOS device name, <= 120 characters);
* duplicates are detected by **content hash**, never by name;
* anything that fails verification goes to `_misfiled/` with a line in
  `_misfiled/MANIFEST.jsonl` saying what it actually is and why it was moved.
  Nothing is ever deleted -- the audit trail is the point.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .verify import Verdict, sha256_of

MISFILED = "_misfiled"
RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_stem(text: str, limit: int = 100) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", text, flags=re.UNICODE)
    text = re.sub(r"_{2,}", "_", text).strip("_. ")
    if not text:
        text = "untitled"
    if text.lower() in RESERVED:
        text = f"{text}_doc"
    return text[:limit].rstrip(". ")


def make_name(author: str, year: str | int, title: str) -> str:
    who = safe_stem(author, 28)
    what = safe_stem(title, 70)
    return f"{who}_{year}_{what}.pdf"


@dataclass
class ImportResult:
    source: Path
    status: str            # stored | rejected | unmatched | duplicate
    title: str = ""
    path: Optional[Path] = None
    note: str = ""

    def line(self) -> str:
        tail = str(self.path) if self.path else self.note
        return f"{self.status:<10} {self.source.name[:52]:<52} {tail}"


@dataclass
class Duplicate:
    kept: Path
    copies: List[Path]


class Library:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    # ---------------------------------------------------------------- scanning
    def pdfs(self) -> List[Path]:
        out = []
        for p in self.root.rglob("*.pdf"):
            if MISFILED in p.parts:
                continue
            out.append(p)
        return sorted(out)

    def hashes(self) -> Dict[str, List[Path]]:
        table: Dict[str, List[Path]] = {}
        for p in self.pdfs():
            table.setdefault(sha256_of(p), []).append(p)
        return table

    def duplicates(self) -> List[Duplicate]:
        out = []
        for digest, paths in sorted(self.hashes().items()):
            if len(paths) > 1:
                out.append(Duplicate(kept=paths[0], copies=paths[1:]))
        return out

    def find_by_hash(self, digest: str, exclude: Optional[Path] = None) -> Optional[Path]:
        """Locate a copy by content. `exclude` matters: when the source already
        lives inside the library, a naive search finds the source itself and the
        "duplicate" short-circuit silently does nothing."""
        skip = exclude.resolve() if exclude else None
        for p in self.pdfs():
            if skip is not None and p.resolve() == skip:
                continue
            if sha256_of(p) == digest:
                return p
        return None

    # -------------------------------------------------------------- operations
    def add_file(
        self,
        src: str | Path,
        author: str,
        year: str | int,
        title: str,
        layer: Optional[str] = None,
        move: bool = True,
    ) -> Path:
        """Place a verified file under a conventional name, refusing duplicates."""
        return self.add_file_ex(src, author, year, title, layer=layer, move=move)[0]

    def add_file_ex(
        self,
        src: str | Path,
        author: str,
        year: str | int,
        title: str,
        layer: Optional[str] = None,
        move: bool = True,
    ) -> tuple:
        """Same, but say whether anything was actually written.

        DoiHunter and scihub-cli both learned to report "skipped, already there"
        separately from "downloaded". Without the distinction a batch prints
        "stored 4/4" while three of the four were already in the library -- which
        is how a reader ends up believing a paper was re-fetched when it was not.
        """
        src = Path(src)
        digest = sha256_of(src)
        existing = self.find_by_hash(digest, exclude=src)
        if existing is not None:
            return existing, False

        target_dir = self.root / layer if layer else self.root
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / make_name(author, year, title)
        n = 2
        while target.exists():
            target = target_dir / f"{safe_stem(target.stem)}_{n}.pdf"
            n += 1
        if move:
            shutil.move(str(src), str(target))
        else:
            shutil.copy2(str(src), str(target))
        return target, True

    def quarantine(self, path: str | Path, reason: str, what_it_is: str = "") -> Path:
        """Move a file (and its .txt sibling) into `_misfiled/`, recording why."""
        path = Path(path)
        qdir = self.root / MISFILED
        qdir.mkdir(exist_ok=True)
        moved = []
        for candidate in (path, path.with_suffix(path.suffix + ".txt"),
                          path.with_suffix(".txt")):
            if candidate.exists():
                dest = qdir / candidate.name
                if dest.exists():
                    dest = qdir / f"{candidate.stem}__{candidate.suffix.lstrip('.')}{candidate.suffix}"
                shutil.move(str(candidate), str(dest))
                moved.append(dest.name)
        record = {
            "moved": moved,
            "reason": reason,
            "what_it_is": what_it_is,
            "original": str(path),
        }
        with open(qdir / "MANIFEST.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return qdir / moved[0] if moved else qdir

    def import_dir(self, directory, refs, layer: Optional[str] = None,
                   copy: bool = True, threshold: float = 0.8,
                   min_bytes: int = 5000):
        """Ingest a folder of hand-downloaded PDFs, matching each to a reference.

        The 2026-10-22 lesson: half the interesting publishers cannot be passed by
        a script, but a human can save the PDF in ten seconds. What a human cannot
        do reliably is name it, prove it, and keep the ledger straight -- so the
        machine does that part.
        """
        from .verify import longest_run, title_block, title_tokens, verify_pdf

        files = sorted(p for p in Path(directory).glob("*") if p.suffix.lower() == ".pdf")
        results = []
        taken = set()
        for f in files:
            text = title_block(f)
            best = None
            for i, ref in enumerate(refs):
                if i in taken:
                    continue
                n = len(title_tokens(ref.title)) or 1
                run = longest_run(ref.title, text) if text else 0
                need = max(3, int(0.6 * n))
                good = run >= need
                score = (1 if good else 0, run / n)
                if best is None or score > best[0]:
                    best = (score, i, ref, good)
            if not best or not best[3]:
                results.append(ImportResult(source=f, status="unmatched",
                                            note="no reference title matches this file"))
                continue
            _, idx, ref, _ = best
            v = verify_pdf(f, expected_title=ref.title,
                           expected_year=ref.year or None, threshold=threshold,
                           min_bytes=min_bytes)
            if not v.ok:
                results.append(ImportResult(source=f, status="rejected",
                                            title=ref.title,
                                            note="; ".join(v.reasons)))
                continue
            dest = self.add_file(f, author=ref.surname() or "anon",
                                 year=ref.year or "nd", title=ref.title,
                                 layer=layer, move=not copy)
            taken.add(idx)
            results.append(ImportResult(source=f, status="stored",
                                        title=ref.title, path=dest))
        return results

    def record_provenance(self, paths, transport: str = "manual-import",
                          tier: str = "manual", title_declared: str = "") -> int:
        """Write provenance lines for files that have none yet.

        Used by `import` and by a one-off sweep of an existing library: a file
        that arrived before this module existed still deserves an honest label.
        """
        from . import provenance

        known = provenance.load(self.root)
        records = []
        for p in paths:
            key = str(Path(p).relative_to(self.root)) if Path(p).is_relative_to(self.root) else str(p)
            if key in known:
                continue
            records.append(provenance.inspect_pdf(
                p, url="", tier=tier, transport=transport,
                title_declared=title_declared))
        return provenance.append(self.root, records) if records else 0

    def quarantine_bad(self, verdict: Verdict, reason: Optional[str] = None) -> Path:
        return self.quarantine(
            verdict.path,
            reason=reason or "; ".join(verdict.reasons) or "failed verification",
            what_it_is=verdict.first_line or f"sniffed as {verdict.kind}",
        )
