"""Identity verification for downloaded documents.

Four conditions, in increasing cost:
  1. the bytes are really a PDF (magic number), not an HTML landing page;
  2. the file parses and has at least one page with text;
  3. the expected title's words cover the first pages at >= `threshold`;
  4. (optional) the expected year appears somewhere in the front matter.

Condition 3 is the load-bearing one. A surname check is *not* enough: two papers
by the same first author (PlaNet vs Director, both Hafner et al.) pass a surname
check and fail a title check. This module therefore keys on the title.

All matching happens on **normalised** text: Unicode NFKD, combining marks and
spacing accents removed, ligatures expanded, lower-cased, punctuation collapsed.
Skipping the ligature step silently zeroes out keyword counts in PDFs that use
`fi`/`fl` glyphs -- a mistake that has already cost this project one audit.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

PDF_MAGIC = b"%PDF"

LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
    "\u0131": "i",  # dotless i
    "\u00a8": "",   # spacing diaeresis, as produced by LaTeX "Hyv¨arinen"
    "\u00b4": "",
    "\u0060": "",
}

STOPWORDS = {
    "a", "an", "the", "of", "on", "in", "for", "and", "or", "to", "with", "by",
    "from", "at", "as", "is", "are", "be", "via", "into", "under", "over",
    "toward", "towards", "using", "based", "new", "some", "its",
}


def normalise(text: str) -> str:
    """Lower-case, de-accent, de-ligature, collapse whitespace."""
    for src, dst in LIGATURES.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(title: str) -> List[str]:
    toks = [t for t in normalise(title).split() if len(t) >= 3 and t not in STOPWORDS]
    return toks


def coverage(expected_title: str, pages_text: str) -> float:
    """Fraction of the title's content words that occur anywhere in `pages_text`.

    Short titles are handled by the caller (`verify_pdf` falls back to a phrase
    check), because a three-word title can reach 1.0 on an unrelated paper.
    """
    toks = title_tokens(expected_title)
    if not toks:
        return 0.0
    hay = normalise(pages_text)
    hit = sum(1 for t in toks if t in hay)
    return hit / len(toks)


def longest_run(expected_title: str, pages_text: str) -> int:
    """Longest *contiguous* run of title words found in the text, in order.

    Word coverage alone is fooled by near-duplicate titles: a follow-up paper
    called "Online Relational Inference for Evolving Multi-agent Interacting
    Systems" contains every word of "Neural Relational Inference for Interacting
    Systems" except one, and scores 1.00 on a set-membership check. Order and
    adjacency are what distinguish a paper from its sequel.
    """
    want = normalise(expected_title).split()
    have = normalise(pages_text).split()
    if not want or not have:
        return 0
    best = 0
    for i in range(len(have)):
        k = 0
        while i + k < len(have) and k < len(want) and have[i + k] == want[k]:
            k += 1
        best = max(best, k)
    return best


def phrase_present(expected_title: str, pages_text: str) -> bool:
    needle = normalise(expected_title)
    return bool(needle) and needle in normalise(pages_text)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", normalise(text))


def squashed_contains(expected_title: str, pages_text: str) -> bool:
    """Is the whole title a contiguous substring once spaces are removed?

    Some typography puts a space between every letter -- old Cyrillic journals
    set titles as spaced capitals ('Т Р И  П О Д Х О Д А ...'), and small-caps
    headings kern the same way. Every word-level test dies on that: the page has
    no token 'подхода', only п|о|д|х|о|д|а.

    The test is *containment of the entire squashed title*, not n-gram overlap.
    That is what keeps it from becoming another loose threshold: the sequel
    'Online Relational Inference for Evolving Multi-agent Interacting Systems'
    does not contain 'neuralrelationalinferenceforinteractingsystems' as a
    substring, so it still fails. Requiring 12 characters keeps a two-word title
    from being "contained" by accident.
    """
    want = _squash(expected_title)
    return len(want) >= 12 and want in _squash(pages_text)


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sniffs(path: Path) -> str:
    """Cheap type sniff that does not need a PDF library."""
    with open(path, "rb") as fh:
        head = fh.read(512)
    if head.startswith(PDF_MAGIC):
        return "pdf"
    stripped = head.lstrip()
    if stripped[:1] == b"<" or b"<html" in stripped[:200].lower():
        return "html"
    if not stripped:
        return "empty"
    return "other"


@dataclass
class Verdict:
    path: str
    ok: bool
    kind: str
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    coverage: float = 0.0
    pages: int = 0
    chars: int = 0
    sha256: str = ""
    first_line: str = ""

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "ok": self.ok,
            "kind": self.kind,
            "reasons": list(self.reasons),
            "coverage": round(self.coverage, 4),
            "pages": self.pages,
            "chars": self.chars,
            "sha256": self.sha256,
            "first_line": self.first_line,
        }

    def __str__(self) -> str:  # pragma: no cover - formatting only
        flag = "OK  " if self.ok else "FAIL"
        return (f"[{flag}] {Path(self.path).name}  kind={self.kind} pages={self.pages} "
                f"chars={self.chars} coverage={self.coverage:.2f}"
                + ("  <- " + "; ".join(self.reasons) if self.reasons else ""))


def _first_line(text: str, limit: int = 90) -> str:
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 8:
            return re.sub(r"\s+", " ", line)[:limit]
    return ""


# Elsevier (and others) put an "Accepted Manuscript" cover sheet in front of the
# article: the largest type on page 1 is the label, and the real title is on
# page 2. Every AAM we hold looked like a wrong file until this was handled.
COVER_LABELS = re.compile(
    r"^(accepted manuscript|author accepted manuscript|post-?print|preprint|"
    r"article in press|original article|research article|review article)\b", re.I)


def _largest_block(page) -> str:
    """Largest-type lines of one page, with stamps and non-text lines filtered out."""
    lines = []
    for b in page.get_text("dict").get("blocks", []):
        for line in b.get("lines", []):
            spans = line.get("spans", [])
            if not spans or tuple(line.get("dir", (1, 0))) != (1.0, 0.0):
                continue                      # skip rotated margin stamps
            size = max(sp.get("size", 0) for sp in spans)
            text = "".join(sp.get("text", "") for sp in spans).strip()
            if not text:
                continue
            if re.match(r"^(arxiv|doi|issn|https?)[:\s]", text, re.I):
                continue                      # the arXiv stamp is set large
            letters = sum(ch.isalpha() for ch in text)
            if letters < 3 or letters < 0.4 * len(text):
                continue
            lines.append((size, text))
    if not lines:
        return ""
    top = max(size for size, _ in lines)
    return " ".join(t for size, t in lines if size >= 0.92 * top and len(t) > 3)[:600]


def title_block(path: Path) -> str:
    """The largest-type lines of the first page that is not a cover sheet.

    Position alone is not enough: a sequel paper cites the original's title in
    its abstract ("unlike Neural Relational Inference for Interacting Systems,
    we ..."), which sits inside the first 1500 characters. Type size is the one
    signal the citing sentence cannot fake -- but on an "Accepted Manuscript"
    cover sheet the largest type is the label, so page 1 is skipped when it says
    so and page 2 is read instead.
    """
    try:
        import pymupdf  # type: ignore
    except Exception:  # pragma: no cover
        try:
            import fitz as pymupdf  # type: ignore
        except Exception:
            return ""
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return ""
    try:
        if not doc.page_count:
            return ""
        for pno in range(min(2, doc.page_count)):
            block = _largest_block(doc[pno])
            if block and not COVER_LABELS.match(block.strip()):
                return block
            if block and pno + 1 >= doc.page_count:
                return block                  # a cover sheet with nothing after it
        return ""
    finally:
        doc.close()


def first_page(path: Path) -> str:
    """Text of page 1 only. Position matters: a citing paper prints the cited
    paper's title in its related-work section, so a phrase found anywhere in the
    first pages proves nothing. The title must be in the *front matter*."""
    try:
        import pymupdf  # type: ignore
    except Exception:  # pragma: no cover
        try:
            import fitz as pymupdf  # type: ignore
        except Exception:
            return ""
    try:
        doc = pymupdf.open(str(path))
        try:
            return doc[0].get_text() if doc.page_count else ""
        finally:
            doc.close()
    except Exception:
        return ""


def front_matter(path: Path, pages: int = 3) -> str:
    """Text of the first `pages` pages, or '' when no PDF backend is available."""
    try:
        import pymupdf  # type: ignore
    except Exception:  # pragma: no cover - depends on the machine
        try:
            import fitz as pymupdf  # type: ignore
        except Exception:
            return ""
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return ""
    try:
        return "".join(doc[i].get_text() for i in range(min(pages, doc.page_count)))
    finally:
        doc.close()


def verify_pdf(
    path: str | Path,
    expected_title: Optional[str] = None,
    expected_year: Optional[str] = None,
    threshold: float = 0.8,
    require_text: bool = False,
    min_bytes: int = 5000,
) -> Verdict:
    """Apply the four conditions. Never raises for malformed input."""
    p = Path(path)
    v = Verdict(path=str(p), ok=False, kind="missing")

    if not p.exists() or not p.is_file():
        v.reasons.append("file does not exist")
        return v

    v.sha256 = sha256_of(p)
    v.kind = sniffs(p)

    if v.kind != "pdf":
        v.reasons.append(f"not a PDF (sniffed as {v.kind})")
        if p.stat().st_size < min_bytes:
            v.reasons.append(f"file is only {p.stat().st_size} bytes")
        return v

    if p.stat().st_size < min_bytes:
        v.reasons.append(f"file is only {p.stat().st_size} bytes")
        return v

    text = front_matter(p, pages=3)
    if not text:
        # No backend or a scanned PDF: we cannot falsify identity, so we refuse
        # to certify it unless the caller explicitly accepts text-less files.
        v.pages = 0
        v.chars = 0
        if require_text:
            v.reasons.append("no extractable text (scanned or no PDF backend)")
            return v
        v.ok = expected_title is None
        v.reasons.append("no extractable text; identity NOT checked")
        return v

    v.chars = len(text)
    v.first_line = _first_line(text)
    try:
        import pymupdf  # type: ignore
        doc = pymupdf.open(str(p))
        v.pages = doc.page_count
        doc.close()
    except Exception:
        v.pages = -1

    if v.chars < 200:
        v.reasons.append(f"almost no text ({v.chars} chars)")
        return v

    if expected_title:
        if len(title_tokens(expected_title)) < 4:
            ok = phrase_present(expected_title, text)
            if ok:
                v.coverage = 1.0
            else:
                # "Toward" vs "Towards" is a spelling variant, not a different
                # paper. Fall back to the scored comparison before rejecting.
                scored = title_score(expected_title, title_block(p) or text[:600])
                v.coverage = scored
                if scored < 0.9:
                    v.reasons.append(
                        f"short title: exact phrase not found (best score {scored:.2f})")
                    return v
                v.warnings.append("short title matched on a spelling variant")
        else:
            v.coverage = coverage(expected_title, text)
            n = len(title_tokens(expected_title))
            need = max(3, int(0.6 * n))
            head = title_block(p) or first_page(p)[:600]
            run = longest_run(expected_title, head)
            if v.coverage < threshold or run < need:
                # Word-level matching fails on letter-spaced typography: the page
                # holds no token 'подхода', only п|о|д|х|о|д|а. Try the whole
                # title as one squashed string before rejecting, and only here --
                # a normally typeset title passes above and never reaches this.
                if squashed_contains(expected_title, text):
                    v.coverage = 1.0
                    v.warnings.append(
                        "word-level check failed but the whole title is a contiguous "
                        "substring once spaces are removed: the front matter sets it "
                        "as single letters or broken fragments")
                elif v.coverage < threshold:
                    v.reasons.append(
                        f"title coverage {v.coverage:.2f} < {threshold:.2f}"
                    )
                    return v
                else:
                    v.reasons.append(
                        "title not in the front matter "
                        f"(longest adjacent run on page 1: {run}/{n}, need {need})"
                    )
                    return v

    if expected_year and expected_year not in text:
        # The year caught a real error on 2026-10-22 and must keep its veto: a
        # file served as "Fan 1991, Annals of Statistics" turned out to be
        # *Technical Report No. 157, June 1988, UC Berkeley* -- the same title,
        # the same author, a different document. An exact title match is exactly
        # what makes that substitution invisible, so the title cannot demote it.
        #
        # The one refinement: a document that prints *no* year at all (an old
        # offprint whose first page is the article itself) is a warning, because
        # there is nothing to contradict. A *different* year is a rejection.
        present = sorted(set(re.findall(r"\b(?:19|20)\d{2}\b", text)))
        if present:
            v.reasons.append(
                f"front matter carries {', '.join(present[:4])} but not {expected_year} "
                "-- a different version of the same title")
            return v
        v.warnings.append(f"expected year {expected_year} not printed in the front matter")
    v.ok = True
    return v


def bulk_verify(
    paths: Iterable[str | Path],
    titles: Optional[Sequence[str]] = None,
    **kwargs,
) -> List[Verdict]:
    out = []
    for i, p in enumerate(paths):
        t = titles[i] if titles and i < len(titles) else None
        out.append(verify_pdf(p, expected_title=t, **kwargs))
    return out


LEADING_STOPWORDS = {"a", "an", "the", "on", "of", "in", "for",
                     "towards", "toward", "about", "re", "note"}


def _run_with_position(want: List[str], got: List[str]) -> tuple:
    """Longest adjacent run of `want` inside `got`, with where it starts."""
    best, best_at = 0, 0
    for i in range(len(got)):
        k = 0
        while i + k < len(got) and k < len(want) and got[i + k] == want[k]:
            k += 1
        if k > best:
            best, best_at = k, i
    return best, best_at


def title_score(expected: str, candidate: str) -> float:
    """How likely is `candidate` the same work as `expected`?  0..1.

    Borrowed from DoiHunter (`--minimum-title-score`, default 0.92): a title is
    resolved to a DOI only when the match clears a threshold, instead of taking
    the first search hit. The code this replaced compared the first 40 characters
    of the normalised titles, which is exactly the shape that accepts a sequel.

    The score mixes two signals because each alone is fooled:
      * token F1 catches paraphrase and word order changes;
      * the longest *adjacent* run catches sequels, which keep the vocabulary and
        break the order.

    Two boundary cases matter and are handled explicitly:
      * words **after** the run are treated as a subtitle -- same work (1.0);
      * words **before** it are treated as a different work, unless they are
        function words. "Conditional neural relational inference for interacting
        systems" is a *different paper* and scored 1.00 under the naive rule;
        "A sparsity-based model of bounded rationality" is the same paper as
        "sparsity-based model of bounded rationality".
    """
    from ..core.verify import title_tokens

    want = title_tokens(expected)
    got = title_tokens(candidate)
    if not want or not got:
        return 0.0

    run, at = _run_with_position(want, got)
    if run >= len(want):
        prefix = got[:at]
        if not prefix:
            return 1.0
        if all(w in LEADING_STOPWORDS for w in prefix):
            return 1.0
        # A content word was prepended: almost always a different article.
        return round(0.5 * len(want) / max(1, len(got)), 3)

    inter = len(set(want) & set(got))
    p = inter / len(got)
    r = inter / len(want)
    f1 = 0.0 if p + r == 0 else 2 * p * r / (p + r)
    return round(0.5 * f1 + 0.5 * run / len(want), 3)
