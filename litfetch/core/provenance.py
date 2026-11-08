"""Where a PDF came from, and *which version of the paper* it is.

Borrowed from `fulltext-article-downloader`, whose README calls the same feature
"honest results": every download reports the route that produced it and carries
a note when the file is a preprint or an accepted manuscript rather than the
publisher's version of record.

The project needed this for a concrete reason. `R141` was fetched from
arXiv:2403.07618 and stored under the name of the *Performance Evaluation*
article. Four conditions passed -- it is the right paper -- but nothing in the
library said the bytes are a preprint. A citation claiming "Thm 4 of the journal
article" would have rested on v3 of the preprint without anyone noticing.

Design rules:

* the record is **append-only** (`PROVENANCE.jsonl`), one line per stored file;
* `version` is one of `preprint` / `accepted` / `vor` / `unknown`, and the
  evidence for it is written next to it -- never a bare label;
* `vor` derived from a publisher host is marked **asserted**, not verified,
  because we did not compare against the journal's own PDF;
* nothing here raises. A missing text layer yields `unknown`, which is a
  truthful answer and the one that should stop a claim.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

PROVENANCE_FILE = "PROVENANCE.jsonl"

# Hosts that serve manuscripts rather than versions of record.
PREPRINT_HOSTS = (
    "arxiv.org",
    "biorxiv.org",
    "medrxiv.org",
    "chemrxiv.org",
    "ssrn.com",
    "researchsquare.com",
    "preprints.org",
    "techrxiv.org",
    "hal.science",           # often the author's deposit, not the VoR
    "econstor.eu",
    "nber.org",              # working papers
    "iza.org",
    "cepr.org",
)

# Hosts that are publishers: a PDF here is *probably* the version of record,
# but we have not compared it with the journal's own copy, so we assert.
PUBLISHER_HOSTS = (
    "wiley.com",
    "onlinelibrary.wiley.com",
    "elsevier.com",
    "sciencedirect.com",
    "springer.com",
    "link.springer.com",
    "tandfonline.com",
    "cambridge.org",
    "oup.com",
    "academic.oup.com",
    "ieee.org",
    "jstor.org",
    "nature.com",
    "science.org",
    "aps.org",
    "aip.org",
    "acs.org",
    "sagepub.com",
    "projecteuclid.org",
    "ijcai.org",
    "mlr.press",
    "proceedings.mlr.press",
    "neurips.cc",
    "openreview.net",
    "acm.org",
    "dl.acm.org",
    "worldscientific.com",
    "degruyter.com",
)

# Phrases that identify an accepted manuscript. Checked before anything else:
# an arXiv deposit of the accepted version is still an accepted manuscript, and
# saying "preprint" there would understate it.
AAM_PATTERNS = (
    r"accepted manuscript",
    r"author accepted manuscript",
    r"this is a pre-copyedited",
    r"accepted for publication",
    r"post-?print",
    r"final draft",
    r"pre-?print of the article",
)

PREPRINT_PATTERNS = (
    r"\barxiv:\s*\d{4}\.\d{4,5}",
    r"\bpre-?print\b",
    r"working paper",
    r"technical report",
    r"discussion paper",
    r"not peer[- ]reviewed",
    r"research square",
    r"\bssrn\b",
)

VOR_PATTERNS = (
    r"version of record",
    r"published in final form",
    r"\bcopyright\s+\u00a9\s+\d{4}",
)


@dataclass
class Provenance:
    file: str = ""
    sha256: str = ""
    retrieved: str = ""
    url: str = ""
    tier: str = ""
    transport: str = ""            # plain urllib | tls-impersonation | chrome-cdp | manual
    version: str = "unknown"        # preprint | accepted | vor | unknown
    version_basis: str = ""         # the evidence, in words
    title_declared: str = ""
    title_printed: str = ""
    coverage: float = 0.0
    pages: int = 0
    note: str = ""

    def line(self) -> str:
        basis = self.version_basis or "no evidence found"
        return (f"{self.version:<9} {Path(self.file).name[:56]:<56} "
                f"[{self.transport or '-'}/{self.tier or '-'}] {basis[:60]}")

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _matches(patterns, text: str) -> str:
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(0).strip()
    return ""


def _page_one(path: Path) -> str:
    """First-page text, lower-cased and whitespace-collapsed. Empty on failure.

    Imported lazily from `verify` so that the module stays useable without
    pymupdf (URL-only classification then remains available).
    """
    try:
        from .verify import first_page
    except Exception:  # pragma: no cover
        return ""
    text = first_page(path)
    return re.sub(r"\s+", " ", text).strip()


FILENAME_TOKENS = ("arxiv", "biorxiv", "medrxiv", "chemrxiv", "ssrn",
                   "preprint", "_wp", "workingpaper")


def classify(url: str = "", text: str = "", in_library_from_publisher: bool = False,
             filename: str = "") -> tuple:
    """Return `(version, basis)` from a URL and the first page of text.

    Precedence is deliberate: a phrase on the page beats the host, because the
    host tells you where the file was *served* and the page tells you what it
    *is*. A publisher can host an accepted manuscript in its "free access" slot.
    """
    host = _host(url)

    aam = _matches(AAM_PATTERNS, text)
    if aam:
        return "accepted", f'page 1 says "{aam}"'

    if any(host == h or host.endswith("." + h) for h in PREPRINT_HOSTS):
        return "preprint", f"served by {host}"

    pre = _matches(PREPRINT_PATTERNS, text)
    if pre:
        return "preprint", f'page 1 says "{pre}"'

    # The file name is our own record of where the file came from: weaker than a
    # page marker, stronger than nothing, and clearly labelled as such.
    low = (filename or "").lower()
    token = next((t for t in FILENAME_TOKENS if t in low), "")
    if token:
        return "preprint", f'file name records a "{token}" download (weak evidence)'

    vor = _matches(VOR_PATTERNS, text)
    if vor and (in_library_from_publisher or any(host.endswith(h) for h in PUBLISHER_HOSTS)):
        return "vor", f'publisher host with page-1 marker "{vor}"'

    if any(host == h or host.endswith("." + h) for h in PUBLISHER_HOSTS):
        return "vor", f"publisher host {host} (asserted, not compared with the journal PDF)"

    if host:
        return "unknown", f"no version marker on page 1; host {host} not classified"
    return "unknown", "no URL and no version marker"


def inspect_pdf(path: str | Path, url: str = "", tier: str = "",
                transport: str = "", title_declared: str = "",
                coverage: float = 0.0) -> Provenance:
    """Build a provenance record for one file."""
    path = Path(path)
    text = _page_one(path)[:4000]
    version, basis = classify(url, text, filename=path.name)

    pages, digest = 0, ""
    try:
        import pymupdf  # type: ignore
        doc = pymupdf.open(str(path))
        pages = doc.page_count
        doc.close()
    except Exception:
        pass
    try:
        from .verify import sha256_of
        digest = sha256_of(path)
    except Exception:
        pass

    printed = ""
    try:
        from .verify import title_block
        printed = title_block(path)
    except Exception:
        pass

    return Provenance(
        file=str(path),
        sha256=digest,
        retrieved=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        url=url,
        tier=tier,
        transport=transport,
        version=version,
        version_basis=basis,
        title_declared=title_declared,
        title_printed=printed[:300],
        coverage=round(float(coverage), 3),
        pages=pages,
    )


def append(library_root: str | Path, records) -> int:
    """Append records to `<library>/PROVENANCE.jsonl`. Returns how many were written.

    Paths are stored **relative to the library**, so the record survives moving or
    renaming the library directory, and so the key can never disagree with itself
    depending on whether the caller passed an absolute or a relative path.
    """
    root = Path(library_root)
    root.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(root / PROVENANCE_FILE, "a", encoding="utf-8") as fh:
        for rec in records:
            if isinstance(rec, Provenance):
                rec.file = _rel(rec.file, root) or rec.file
                fh.write(rec.to_json() + "\n")
                n += 1
    return n


def compact(library_root: str | Path) -> tuple:
    """Rewrite the file with one line per file (the latest). Returns (before, after)."""
    root = Path(library_root)
    path = root / PROVENANCE_FILE
    if not path.exists():
        return 0, 0
    before = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    latest = load(root)
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for key in sorted(latest):
            rec = dict(latest[key])
            rec["file"] = key
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return before, len(latest)


def load(library_root: str | Path) -> dict:
    """Latest record per file (relative path), keyed so it survives a move within the library."""
    root = Path(library_root)
    path = root / PROVENANCE_FILE
    latest: dict = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = _rel(rec.get("file", ""), root)
        if key:
            latest[key] = rec
    return latest


def _rel(filename: str, root: Path) -> str:
    """Path relative to the library root, resolved on both sides.

    Comparing an absolute record path with a relative root silently degrades to
    the bare file name, and two different layers then collide on one key -- which
    is how a 159-file ledger reported 142 entries and a re-recorded file appeared
    twice under two spellings of the same path.
    """
    if not filename:
        return ""
    p = Path(filename)
    if not p.is_absolute():
        p = root / p          # a record written relative to the library, not the cwd
    try:
        # POSIX separators, always. A library recorded on Windows must produce the
        # same keys as one recorded on macOS, or the ledger stops matching the
        # files as soon as the folder moves between machines.
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return p.name


def summarise(records: dict) -> dict:
    counts = {"preprint": 0, "accepted": 0, "vor": 0, "unknown": 0}
    for rec in records.values():
        v = rec.get("version", "unknown")
        counts[v if v in counts else "unknown"] += 1
    return counts


def missing(library_paths, records: dict) -> list:
    """Files with no provenance line at all -- i.e. what predates this module."""
    return [p for p in library_paths if str(p) not in records]
