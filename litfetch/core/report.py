"""The end-of-run answer: what happened, and what to do about it.

Per-reference lines already say why *that* reference failed, but a batch of 125
refuses to be read one line at a time. The question at the end of a run is a
different one -- "what do I do next?" -- and it only has five or six possible
answers:

    bot wall      a human browser is required
    needs JS      retry with --browser, or import by hand
    landing page  the URL was an abstract page, not a PDF
    no access     paywall; this network will never get it
    rejected      bytes arrived but were not the right paper
    ...

So the run ends with a histogram of those, not just a count of failures. It also
names the tiers that were *never attempted*, because a silently skipped tier
looks exactly like "there is no open copy" -- and on the 2026-11 batch that
mistake cost two hours of running a browser tier that could not help while
Unpaywall was disabled for want of a contact address.

`build_report` is pure: it takes outcomes and returns a dict. That keeps the
numbers testable without a network, and lets `--json` print the same object the
human summary is rendered from.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, Iterable, List, Optional

# reason key -> (label, action). The action column is the point of the table:
# each failure has exactly one useful next step, and they are not interchangeable.
REASONS = {
    "cloudflare": ("bot wall (Cloudflare)",
                   "open in your own browser, then `litfetch import`"),
    "botwall": ("bot wall (Imperva/Incapsula)",
                "open in your own browser, then `litfetch import`"),
    "challenge": ("human-verification challenge",
                  "open in your own browser, then `litfetch import`"),
    "paywall": ("paywall / no entitlement",
                "use your library proxy or inter-library loan"),
    "js_required": ("publisher page needs JS",
                    "retry with --browser, or import by hand"),
    "not_pdf": ("landing page, not a PDF",
                "supply the PDF URL; abstract pages rarely serve one"),
    "dead": ("dead link (404/410)",
             "find another copy (OpenAlex / Semantic Scholar / author page)"),
    "error": ("transport error / timeout",
              "retry once; if it repeats, treat as dead"),
    "identity": ("rejected: page-1 identity check",
                 "open it; usually another version or a same-title paper"),
    "version": ("rejected: version requirement",
                "accept the version you have, or drop --version-required"),
    "no_url": ("no candidate URL",
               "add a doi/url to the reference, or enable --browser"),
    "budget": ("budget exhausted",
               "raise --budget, or rerun the rest with --resume"),
    "browser": ("browser tier could not grab it",
                "save it in your own browser, then `litfetch import`"),
    "skipped": ("skipped (already done)", ""),
    "dry-run": ("dry run (nothing fetched)", ""),
    "other": ("unclassified", "read the per-reference note"),
}

# The verdicts `io.diagnose.classify` can return; a note that starts with one of
# these is a diagnosis, not free text.
CODES = {"cloudflare", "botwall", "challenge", "paywall", "js_required",
         "dead", "not_pdf", "error", "pdf"}

DONE = {"stored", "duplicate"}
NOT_A_GAP = DONE | {"skipped", "planned"}


def reason_of(outcome) -> str:
    """One stable key per non-stored outcome. Order of evidence matters."""
    status = getattr(outcome, "status", "") or ""
    if status in DONE:
        return ""
    if status == "skipped":
        return "skipped"
    if status == "planned":
        return "dry-run"

    note = (getattr(outcome, "note", "") or "").strip()
    if status == "rejected":
        # Provenance refused a real paper: the bytes were right, the version was not.
        return "version" if note.startswith("version is") else "identity"
    blocked = getattr(outcome, "blocked", "") or ""
    if blocked in CODES:
        return blocked

    head = note.split(":", 1)[0].strip().lower()
    if head in CODES:
        return head
    if note.startswith("no candidate URL"):
        return "no_url"
    if note.startswith("budget of"):
        return "budget"
    if (getattr(outcome, "level", "") or "") == "L3":
        # Whatever the browser tier said, the answer is the same: do it by hand.
        return "browser"
    return "other"


def label_action(reason: str):
    return REASONS.get(reason, REASONS["other"])


def build_report(outcomes: Iterable, *, library: str = "", log: str = "",
                 only: str = "", resume: bool = False,
                 skipped_tiers: Optional[List[dict]] = None) -> Dict:
    outcomes = list(outcomes)
    counts = Counter(getattr(o, "status", "") or "unknown" for o in outcomes)

    reasons: Counter = Counter()
    gaps: List[dict] = []
    versions: Dict[str, int] = {}
    for o in outcomes:
        if o.version:
            versions[o.version] = versions.get(o.version, 0) + 1
        reason = reason_of(o)
        if not reason or getattr(o, "status", "") in NOT_A_GAP:
            continue
        reasons[reason] += 1
        label, action = label_action(reason)
        gaps.append({
            "ref": str(getattr(getattr(o, "ref", None), "num", "") or ""),
            "title": (getattr(o, "title", "") or "")[:160],
            "status": getattr(o, "status", ""),
            "reason": reason,
            "label": label,
            "action": action,
            "detail": (getattr(o, "note", "") or "")[:200],
        })

    return {
        "refs": len(outcomes),
        "counts": {k: counts[k] for k in ("stored", "duplicate", "skipped",
                                         "blocked", "rejected", "unavailable",
                                         "planned") if counts[k]},
        "stored": counts["stored"],
        "gaps": len(gaps),
        "reasons": [{"reason": k, "label": label_action(k)[0],
                     "action": label_action(k)[1], "count": v}
                    for k, v in reasons.most_common()],
        "versions": dict(sorted(versions.items())),
        "skipped_tiers": list(skipped_tiers or []),
        "not_stored": gaps,
        "library": library,
        "log": log,
        "only": only,
        "resume": bool(resume),
    }


def format_report(report: Dict) -> str:
    """The human half of the report. `--json` prints the same object."""
    lines: List[str] = []
    c = report.get("counts", {})
    line = ("stored {s}  duplicate {d}  skipped {k}  blocked {b}  "
            "rejected {r}  unavailable {u}".format(
                s=c.get("stored", 0), d=c.get("duplicate", 0),
                k=c.get("skipped", 0), b=c.get("blocked", 0),
                r=c.get("rejected", 0), u=c.get("unavailable", 0)))
    if c.get("planned"):
        # A dry run would otherwise end in six zeroes, which reads like a run
        # that did nothing rather than one that planned everything.
        line += f"  planned {c['planned']}"
    lines.append(line)
    versions = report.get("versions") or {}
    if versions:
        lines.append("versions: " + "  ".join(f"{k}={v}" for k, v in versions.items()))

    reasons = report.get("reasons") or []
    if reasons:
        total = sum(r["count"] for r in reasons)
        lines.append("")
        lines.append(f"why {total} of {report.get('refs', 0)} were not stored:")
        width = max(len(r["label"]) for r in reasons)
        for r in reasons:
            lines.append(f"  {r['count']:>4}  {r['label']:<{width}}  {r['action']}".rstrip())

    tiers = report.get("skipped_tiers") or []
    if tiers and reasons:
        lines.append("")
        lines.append("not attempted:")
        for t in tiers:
            lines.append(f"  {t.get('tier', ''):<6}{t.get('why', '')}".rstrip())
            if t.get("fix"):
                lines.append(f"          fix: {t['fix']}")
    return "\n".join(lines)
