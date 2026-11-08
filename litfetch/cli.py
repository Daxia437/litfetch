"""Command line interface.

    litfetch doctor                        what this machine can and cannot do
    litfetch verify FILE [--title T]       apply the four conditions to one file
    litfetch scan LIBRARY                  duplicates, non-PDFs, wrong-name files
    litfetch diagnose --doi DOI            why a download fails, and what to do next
    litfetch resolve --title T --rank      where we would get it, and why that DOI
    litfetch discover --title T --web      copies outside the OA indexes (author pages)
    litfetch classify refs.csv --plan P    triage a list by reachability (auto vs office/browser)
    litfetch fetch refs.json --library L   the tiered loop, with verification
    litfetch import FILE|--from-dir ...    ingest hand downloads, same checks
    litfetch versions --library L          preprint / accepted / version of record
    litfetch quarantine FILE --library L --reason ...
    litfetch ledger --library L [--bibliography review.md]
    litfetch skill install|--check          the bundled agent skill
    litfetch schema [--write PATH]          the evidence-record JSON Schema

Everything is intentionally boring: no config file, no state, no daemon. A tool
that has to be installed on three operating systems must not need explaining.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import List

from . import __version__
from .core import audit as audit_mod
from .core import ledger, model, provenance, refs as refs_mod
from .core import report as report_mod
from .core.select import select as select_refs, suggest as suggest_ids
from .io import diagnose as diag, sources
from .io import reachability as reach
from .io.fetch import fetch_ref, tls_available, verified_refs as fetch_verified
from .core.store import Library
from .core.verify import front_matter, sniffs, verify_pdf


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def cmd_doctor(args) -> int:
    info = {
        "litfetch": __version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
        "mailto_set": bool(sources.DEFAULT_MAILTO),
        "library": str(Path(args.library).expanduser().resolve()) if args.library else None,
    }
    try:
        import pymupdf  # noqa: F401
        info["pdf_backend"] = "pymupdf"
    except Exception:
        try:
            import fitz  # noqa: F401
            info["pdf_backend"] = "pymupdf (fitz alias)"
        except Exception:
            info["pdf_backend"] = None
    try:
        import websocket  # noqa: F401
        info["browser_backend"] = "websocket-client"
    except Exception:
        info["browser_backend"] = None
    from .io.browser import find_chrome
    from .io.fetch import tls_available
    info["chrome"] = find_chrome()
    info["tls_impersonation"] = tls_available()
    if info["library"]:
        lib = Library(args.library)
        info["pdfs"] = len(lib.pdfs())
    _print(info)
    # Two severities, because they answer different questions. `fatal` means the
    # tool cannot do its job: without a PDF backend identity checks collapse to
    # magic-byte and size, which is the one degradation we refuse to call fine.
    # `degraded` means a documented optional extra is absent -- the README says
    # the package still runs, so doctor must not call that an environment
    # failure, or `litfetch doctor` becomes unusable as a CI gate on a minimal
    # install (which is exactly how the first public CI run failed).
    fatal, degraded = [], []
    if not info["pdf_backend"]:
        fatal.append("no PDF backend: identity checks degrade to magic-byte only "
                     "(pip install pymupdf)")
    if not info["mailto_set"]:
        degraded.append("LITFETCH_MAILTO unset: Unpaywall tier is skipped "
                        "(set it to your email; the APIs ask for one)")
    if not info.get("tls_impersonation"):
        degraded.append("curl_cffi absent: no TLS-fingerprint retry "
                        "(pip install 'litfetch[tls]'; it does NOT defeat Cloudflare)")
    for msg in fatal:
        print("x " + msg, file=sys.stderr)
    for msg in degraded:
        print("! " + msg, file=sys.stderr)
    if fatal:
        return 2
    if degraded and getattr(args, "strict", False):
        return 2
    return 0


def cmd_verify(args) -> int:
    v = verify_pdf(args.file, expected_title=args.title, expected_year=args.year,
                   threshold=args.threshold, require_text=args.require_text)
    if args.json:
        _print(v.as_dict())
    else:
        print(v)
        if v.first_line:
            print(f"       first line: {v.first_line}")
    return 0 if v.ok else 1


def cmd_diagnose(args) -> int:
    ref = sources.Reference(num="1", title=args.title or "", year=args.year or "",
                            authors=args.authors or "", doi=args.doi or "",
                            urls=args.url or [])
    if not (ref.title or ref.doi):
        print("need --doi or --title", file=sys.stderr)
        return 2
    if ref.doi and ref.title:
        ok, actual, ratio = sources.verify_id(ref.doi, ref.title, timeout=args.timeout)
        if not ok:
            print(f"! the id does not point at that title (match {ratio}): {actual}",
                  file=sys.stderr)
    res = sources.resolve(ref, use_arxiv=not args.no_arxiv)
    probes = diag.diagnose(res, timeout=args.timeout)
    report = {"title": res.canonical_title, "doi": res.doi,
              "summary": diag.summarise(probes),
              "probes": [p.as_dict() for p in probes]}
    if args.json:
        _print(report)
    else:
        print(f"{res.canonical_title}  ({res.year})")
        for p in probes:
            print(f"  {p.verdict:<11} {p.status:>3} {p.size:>7}B  {p.url[:78]}")
            print(f"              -> {p.action}")
        print(f"\nsummary: {report['summary']}")
    return 0 if any(p.verdict == "pdf" for p in probes) else 1


def cmd_discover(args) -> int:
    """Find candidate copies beyond the OA indexes, including an author's own page.

    The gap this closes: OpenAlex and Unpaywall index repositories, not personal
    homepages, and a great many older papers exist only on the author's site.
    This command gathers candidates, probes each one, and prints a `urls` block
    that can be pasted into refs.json -- where the LA tier takes over and every
    candidate is verified against the title block before it is stored.

    When the search engine is unreachable (it often is, from a script), the note
    says so; the URLs can be supplied by hand instead.
    """
    ref = sources.Reference(num=args.num or "1", title=args.title or "",
                            year=args.year or "", authors=args.authors or "",
                            doi=args.doi or "")
    if not (ref.title or ref.doi):
        print("need --title or --doi", file=sys.stderr)
        return 2

    res = sources.resolve(ref, use_arxiv=not args.no_arxiv)
    candidates = list(res.ordered())

    extra = []
    if res.doi:
        extra += [("semantic-scholar", u) for u in sources.semanticscholar_pdf(res.doi)]
    note = ""
    if args.web and ref.title:
        found, note = sources.web_search_pdfs(ref.title, rows=args.rows)
        extra += [("web-search", u) for u in found]

    seen = {u for _, u in candidates}
    for source, u in extra:
        if u not in seen:
            seen.add(u)
            candidates.append((source, u))

    probes = []
    for tier, u in candidates:
        p = diag.probe(u, timeout=args.timeout)
        p.detail = f"[{tier}] {p.detail}"
        probes.append(p)

    usable = [p.url for p in probes if p.verdict == "pdf"]

    if args.json:
        _print({"title": res.canonical_title, "doi": res.doi,
                "note": note, "usable": usable,
                "candidates": [p.as_dict() for p in probes]})
        return 0 if usable else 1

    print(f"{res.canonical_title}  ({res.year})")
    if res.doi:
        print(f"doi: {res.doi}")
    for p in probes:
        print(f"  {p.verdict:<11} {p.size:>7}B  {p.url[:96]}")
    if note:
        print(f"note: {note}")
    if usable:
        print("\nurls for refs.json (tried at the LA tier, verified like any other):")
        print("  " + json.dumps(usable, ensure_ascii=False))
    else:
        print("\nno reachable PDF among the candidates; "
              "open one in a browser and use `litfetch import`")
    return 0 if usable else 1


def cmd_scan(args) -> int:
    lib = Library(args.library)
    dup = lib.duplicates()
    bad = []
    for p in lib.pdfs():
        if sniffs(p) != "pdf":
            bad.append((str(p.relative_to(lib.root)), sniffs(p)))
    report = {
        "pdfs": len(lib.pdfs()),
        "duplicate_groups": len(dup),
        "duplicate_copies": sum(len(d.copies) for d in dup),
        "non_pdf": bad,
    }
    if args.json:
        _print(report)
    else:
        print(f"pdfs            : {report['pdfs']}")
        print(f"duplicate groups: {report['duplicate_groups']} "
              f"({report['duplicate_copies']} extra copies)")
        print(f"not a PDF       : {len(bad)}")
        for name, kind in bad:
            print(f"  - {name}  [{kind}]")
        if args.verbose:
            for d in dup:
                print(f"  = {d.kept.relative_to(lib.root)}")
                for c in d.copies:
                    print(f"      copy: {c.relative_to(lib.root)}")
    return 1 if bad else 0


def cmd_resolve(args) -> int:
    ref = sources.Reference(num="1", title=args.title or "", year=args.year or "",
                            authors=args.authors or "", doi=args.doi or "",
                            urls=args.url or [])
    if not (ref.title or ref.doi):
        print("need --doi or --title", file=sys.stderr)
        return 2

    ranking = []
    if ref.title and args.rank:
        ranking = sources.resolve_title(ref.title, min_score=args.min_score)

    res = sources.resolve(ref, use_arxiv=not args.no_arxiv)
    out = {"title": res.canonical_title, "year": res.year, "doi": res.doi,
           "tiers": res.tiers, "notes": res.notes,
           "ranking": ranking}
    if args.json:
        _print(out)
        return 0

    print(f"{res.canonical_title}  ({res.year})")
    if res.doi:
        print(f"doi: {res.doi}")
    for note in res.notes:
        print(f"note: {note}")
    if ranking:
        print("\ntitle candidates (score >= {:.2f} accepted):".format(args.min_score))
        for c in ranking[:8]:
            mark = "yes" if c["accepted"] else " NO"
            src = "+".join(c["sources"])
            doi = c["doi"] or "(no doi)"
            print(f"  {c['score']:.2f} {mark}  {src:<18} {doi:<32} {c['title'][:64]}")
        print()
    for level, url in res.ordered():
        print(f"  {level}  {url}")
    if not res.ordered():
        print("  (no candidate URL: go manual)")
    return 0


def _load_refs(path: Path) -> List[sources.Reference]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("refs", [])
    return [sources.Reference.from_dict(d) for d in data]


def _load_refs_any(path: Path) -> List[sources.Reference]:
    """refs.json (litfetch format) or a crossscale-style CSV (CID, link, title_hint)."""
    if path.suffix.lower() == ".csv":
        import csv
        out = []
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                num = row.get("CID") or row.get("num") or row.get("id") or ""
                link = row.get("link") or row.get("url") or row.get("URL") or ""
                title = (row.get("title_hint") or row.get("title")
                         or row.get("Title") or "")
                doi = row.get("doi") or row.get("DOI") or ""
                urls = [link] if link else []
                out.append(sources.Reference(num=str(num), title=title or "",
                                            year="", authors="", doi=doi or "",
                                            urls=urls))
        return out
    return _load_refs(path)


def cmd_classify(args) -> int:
    """Classify a reference list by how each PDF can be obtained, and optionally
    auto-fetch the ones this sandbox can reach.

    This is the planning step that used to live in a project-specific script. It is
    now a first-class tier: given refs (a litfetch refs.json, or a crossscale-style
    CSV with CID/link/title_hint), it prints a verdict per reference --

        AUTO        this sandbox can fetch it (curl + byte-level verify)
        OA_COPY     paywalled DOI, but an OA preprint was found (--oa-lookup)
        OFFICE_SUB  subscription wall, needs institution credentials
        BROWSER     open access but JS/anti-bot gated, needs a real browser
        REPO        code/dataset repository, git clone (not a PDF)
        ALT_SOURCE  source known corrupt, fetch an alternate copy
        UNKNOWN     could not be classified

    With --fetch, the AUTO and OA_COPY references are downloaded into --library
    with the same four-condition verification as every other tier; the rest are
    reported, not attempted. --oa-lookup adds network calls to OpenAlex /
    Unpaywall / Crossref, so plain classify stays static and offline-reproducible.
    """
    all_refs = _load_refs_any(Path(args.refs))
    refs, _sel, unmatched = select_refs(all_refs, args.only)
    if args.only:
        if not refs:
            print(f"--only {args.only!r} matched none of the {len(all_refs)} references",
                  file=sys.stderr)
            sample = ", ".join(str(r.num) for r in all_refs[:8])
            if sample:
                print(f"known ids include: {sample}", file=sys.stderr)
            return 2
        for term in unmatched:
            near = suggest_ids(term, [r.num for r in all_refs])
            hint = f" (did you mean {', '.join(near)}?)" if near else ""
            print(f"warning: --only term {term!r} matched nothing{hint}", file=sys.stderr)
    broken = {s.strip() for s in (args.known_broken or "").split(",") if s.strip()}

    rows: List[reach.Verdict] = []
    for ref in refs:
        if str(ref.num) in broken:
            rows.append(reach.Verdict(
                str(ref.num), ref.title or "",
                (ref.urls[0] if ref.urls else ""), reach.ALT_SOURCE, [],
                "source known corrupt; need an alternate copy"))
        else:
            doi = ref.doi or ""
            if not doi and ref.urls and "doi.org/" in ref.urls[0]:
                doi = ref.urls[0].split("doi.org/", 1)[-1]
            rows.append(reach.classify(str(ref.num), ref.title or "",
                                      list(ref.urls or []), doi,
                                      oa_lookup=args.oa_lookup,
                                      mailto=args.mailto or ""))

    # tally
    from collections import Counter
    tally = Counter(r.verdict for r in rows)

    # write the plan
    if args.plan:
        import csv as _csv
        with open(args.plan, "w", newline="", encoding="utf-8") as fh:
            w = _csv.writer(fh)
            w.writerow(["CID", "verdict", "title", "link", "note", "candidates"])
            for r in rows:
                w.writerow([r.num, r.verdict, r.title, r.link, r.note,
                            json.dumps([c.__dict__ for c in r.candidates],
                                       ensure_ascii=False)])
        print(f"plan written: {args.plan}")

    # print table
    if args.json:
        _print([r.as_dict() for r in rows])
    else:
        print(f"{'CID':<8} {'verdict':<11} {'note'}")
        for r in rows:
            print(f"{r.num:<8} {r.verdict:<11} {r.note[:78]}")
        print("\n" + "  ".join(f"{k} {v}" for k, v in sorted(tally.items())))

    # optional auto-fetch of the AUTO / OA_COPY ones
    if args.fetch:
        if not args.library:
            print("--fetch needs --library", file=sys.stderr)
            return 2
        lib = Library(args.library)
        stored = rejected = failed = 0
        for r in rows:
            if r.verdict not in (reach.AUTO, reach.OA_COPY):
                continue
            # An OA_COPY is a preprint standing in for a journal DOI: its identity
            # MUST be checked against the DOI's registered title, never against a
            # hint that may just be a URL. Refuse to store if we cannot.
            declared = r.title
            if r.verdict == reach.OA_COPY:
                m = re.search(r"declared_title=(.*)$", r.note)
                declared = (m.group(1).strip() if m else "") or r.title
            expected = declared if declared and len(declared) >= 20 else r.title
            if r.verdict == reach.OA_COPY and (not expected or len(expected) < 20):
                print(f"  {r.num:<8} OA-REFUSED (no DOI title to verify against)")
                rejected += 1
                continue
            res = reach.auto_land(r.candidates, timeout=args.timeout)
            if not res:
                print(f"  {r.num:<8} AUTO-FAIL (no valid PDF obtained)")
                failed += 1
                continue
            body, pages, used = res
            tmp = Path(args.library).expanduser() / f".tmp_{r.num}.pdf"
            tmp.write_bytes(body)
            # OA_COPY always verifies (identity is the whole point); AUTO verifies
            # only when the supplied title looks like a real title.
            do_verify = (args.verify and len(expected or "") >= 20) or \
                        r.verdict == reach.OA_COPY
            ok = True
            cov = 0.0
            if do_verify:
                vp = verify_pdf(str(tmp), expected_title=expected,
                                expected_year=None, threshold=args.threshold)
                ok = vp.ok
                cov = getattr(vp, "coverage", 0.0)
            if ok or args.force:
                name_title = expected or r.title or r.num
                author = (name_title.split()[0] if name_title else "anon")
                dest, created = lib.add_file_ex(
                    str(tmp), author=author, year="nd", title=name_title,
                    layer=args.layer, move=True)
                # A duplicate short-circuit returns without moving `src`, so the
                # scratch file would otherwise be left in the library root --
                # where `rglob("*.pdf")` then counts it as a real holding.
                tmp.unlink(missing_ok=True)
                rec = provenance.inspect_pdf(
                    dest, url=used,
                    tier="OA" if r.verdict == reach.OA_COPY else "auto",
                    transport="oa-arxiv" if r.verdict == reach.OA_COPY else "curl",
                    title_declared=name_title, coverage=cov)
                if r.verdict == reach.OA_COPY:
                    rec.note = ("OA preprint standing in for a paywalled journal DOI; "
                                "identity verified against the Crossref/OpenAlex title. "
                                "VERSION=preprint, NOT version of record.")
                provenance.append(lib.root, [rec])
                print(f"  {r.num:<8} {'OA-OK' if r.verdict == reach.OA_COPY else 'AUTO-OK'}"
                      f" {pages}p cov={cov:.2f} -> {Path(dest).name}"
                      f"{'' if created else ' (already in library)'}")
                stored += 1
            else:
                tmp.unlink(missing_ok=True)
                print(f"  {r.num:<8} REJECTED (title mismatch, use --force)")
                rejected += 1
        print(f"\nstored {stored}  rejected {rejected}  auto-fail {failed}")
        return 0 if stored else 1

    return 0


def _skipped_tiers(args) -> List[dict]:
    """Tiers that were never attempted, with the one-line fix for each.

    Silent skips are the expensive kind: an unset `LITFETCH_MAILTO` makes the
    Unpaywall tier return no candidates at all, which in a plain report is
    indistinguishable from "no open copy exists".
    """
    out: List[dict] = []
    mailto = os.environ.get("LITFETCH_MAILTO") or sources.DEFAULT_MAILTO
    if not mailto:
        out.append({"tier": "L1",
                    "why": "Unpaywall has no contact address, so it contributed no "
                           "candidates at all",
                    "fix": "export LITFETCH_MAILTO=you@example.org"})
    if not getattr(args, "browser", False):
        out.append({"tier": "L3", "why": "Chrome tier not enabled",
                    "fix": "add --browser"})
    if getattr(args, "no_tls", False):
        out.append({"tier": "tls", "why": "TLS-fingerprint retry disabled by --no-tls",
                    "fix": ""})
    elif not tls_available():
        out.append({"tier": "tls",
                    "why": "TLS-fingerprint retry unavailable (curl_cffi not installed)",
                    "fix": "pip install 'litfetch[tls]'"})
    if getattr(args, "tiers", None):
        out.append({"tier": "tiers", "why": f"restricted to {args.tiers}", "fix": ""})
    return out


def cmd_fetch(args) -> int:
    all_refs = _load_refs(Path(args.refs))
    refs, _selector, unmatched = select_refs(all_refs, args.only)
    if args.only:
        if not refs:
            print(f"--only {args.only!r} matched none of the {len(all_refs)} references",
                  file=sys.stderr)
            sample = ", ".join(str(r.num) for r in all_refs[:8])
            if sample:
                print(f"known ids include: {sample}", file=sys.stderr)
            return 2
        for term in unmatched:
            near = suggest_ids(term, [r.num for r in all_refs])
            hint = f" (did you mean {', '.join(near)}?)" if near else ""
            print(f"warning: --only term {term!r} matched nothing{hint}", file=sys.stderr)
    lib = Library(args.library)
    # With --json, stdout carries the report object and nothing else; the human
    # lines go to stderr so `... | jq` works.
    emit = sys.stderr if getattr(args, "json", False) else sys.stdout

    done = set()
    if args.resume:
        if not args.log:
            print("--resume needs --log (the attempt log is the record of what finished)",
                  file=sys.stderr)
            return 2
        done = fetch_verified(args.log)

    outcomes = []
    for ref in refs:
        if str(ref.num) in done:
            from .io.fetch import Outcome
            outcomes.append(Outcome(ref=ref, status="skipped", level="log",
                                    title=ref.title,
                                    note="verified in an earlier run (--resume)"))
            print(outcomes[-1].line(), file=emit, flush=True)
            continue
        outcomes.append(fetch_ref(
            ref, lib, layer=args.layer, use_browser=args.browser,
            threshold=args.threshold, dry_run=args.dry_run,
            require_text=args.require_text,
            tiers_allowed=args.tiers.split(",") if args.tiers else None,
            timeout=args.timeout, max_seconds=args.budget,
            verbose=args.verbose, log_path=args.log,
            allow_tls=not args.no_tls,
            version_required=args.version_required,
            skip_existing=args.skip_existing,
            min_bytes=args.min_bytes,
        ))
        print(outcomes[-1].line(), file=emit, flush=True)

    def count(status):
        return sum(1 for o in outcomes if o.status == status)

    report = report_mod.build_report(
        outcomes, library=args.library, log=args.log or "",
        only=args.only or "", resume=args.resume,
        skipped_tiers=_skipped_tiers(args),
    )
    print("\n" + report_mod.format_report(report), file=emit)

    blocked = [o for o in outcomes if o.status == "blocked"]
    if blocked:
        print("blocked by bot walls (open these in your own browser, then `litfetch import`):",
              file=emit)
        for o in blocked:
            print(f"  {o.ref.num:>4}  {o.blocked:<10} {o.title[:60]}", file=emit)

    # The attempt log is append-only evidence. The run summary goes beside it,
    # never on top of it: an earlier version overwrote the log at the end of
    # every successful run, destroying exactly the evidence it existed to keep.
    if args.log:
        summary_path = Path(args.log).with_suffix(Path(args.log).suffix + ".summary.json")
        summary_path.write_text(
            json.dumps([{"ref": o.ref.num, "status": o.status, "level": o.level,
                         "path": o.path, "note": o.note, "version": o.version,
                         "blocked": o.blocked,
                         "verdict": o.verdict.as_dict() if o.verdict else None}
                        for o in outcomes], ensure_ascii=False, indent=1),
            encoding="utf-8")
    if getattr(args, "json", False):
        # The same object the human summary was rendered from, so a caller can
        # branch on `reasons[].reason` instead of scraping prose.
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if count("stored") or count("duplicate") else 1


def cmd_import(args) -> int:
    """Ingest files a human downloaded, with the same verification as a fetch.

    This is the honest end of the ladder: when a publisher's bot wall cannot be
    passed by any automated tier, the user opens the link in their own browser
    and saves the PDF. `import` then does the part that machines are good at --
    proving the file is the right paper and naming it consistently. With
    `--from-dir` and a refs.json it matches a whole folder of downloads against
    the bibliography, so the human never has to name anything.
    """
    if args.from_dir:
        if not args.refs:
            print("--from-dir needs --refs", file=sys.stderr)
            return 2
        refs = _load_refs(Path(args.refs))
        lib = Library(args.library)
        results = lib.import_dir(args.from_dir, refs, layer=args.layer,
                                 copy=args.copy, threshold=args.threshold)
        for r in results:
            print(r.line())
        stored = sum(1 for r in results if r.status == "stored")
        print(f"\nstored {stored}/{len(results)}")
        return 0 if stored == len(results) else 1

    if not args.file:
        print("need a FILE or --from-dir", file=sys.stderr)
        return 2
    if not args.title:
        print("need --title (what the file claims to be)", file=sys.stderr)
        return 2

    v = verify_pdf(args.file, expected_title=args.title,
                   expected_year=args.year or None, threshold=args.threshold)
    if not v.ok and not args.force:
        print(v)
        print("refusing to import: " + "; ".join(v.reasons), file=sys.stderr)
        return 1
    lib = Library(args.library)
    if not v.ok:
        dest = lib.quarantine(args.file, reason="; ".join(v.reasons),
                              what_it_is=v.first_line or v.kind)
        print(f"forced import into quarantine: {dest}")
        return 0
    dest, created = lib.add_file_ex(args.file, author=args.authors or "anon",
                                    year=args.year or "nd",
                                    title=args.title, layer=args.layer,
                                    move=not args.copy)
    if created:
        rec = provenance.inspect_pdf(dest, url=args.source_url or "",
                                     tier="manual", transport="manual-import",
                                     title_declared=args.title,
                                     coverage=v.coverage)
        provenance.append(lib.root, [rec])
        print(f"stored: {dest}")
        print(f"version: {rec.version}  ({rec.version_basis})")
    else:
        print(f"already in the library: {dest}")
    return 0


def _load_entries(path: Path):
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".bib", ".bibtex"):
        return audit_mod.entries_from_bibtex(text)
    if suffix == ".json":
        return audit_mod.entries_from_csl_json(text)
    return audit_mod.entries_from_markdown(text)


def cmd_audit(args) -> int:
    """Is there a file for every entry, and is each file the right paper?"""
    if args.bibliography:
        source = Path(args.bibliography)
    elif args.zotero:
        source = Path(args.zotero)
    else:
        print("need --bibliography review.md or --zotero library.json|.bib",
              file=sys.stderr)
        return 2

    entries = _load_entries(source)
    if not entries:
        print(f"no entries parsed from {source}", file=sys.stderr)
        return 2

    lib = Library(args.library)
    aliases, problems = audit_mod.load_aliases(lib.root)
    for problem in problems:
        print(f"aliases: {problem}", file=sys.stderr)
    rows = audit_mod.audit(lib, entries, threshold=args.threshold,
                           limit=args.limit,
                           skip_unverifiable=args.skip_unverifiable,
                           aliases=aliases)
    counts = audit_mod.summarise(rows)

    if args.json:
        _print({"entries": len(entries), "counts": counts,
                "rows": [r.__dict__ for r in rows]})
    else:
        print(audit_mod.render(rows, title=f"Audit of {source.name} against {args.library}"))
        print("\n" + "  ".join(f"{k} {v}" for k, v in counts.items()))

    if args.report:
        Path(args.report).write_text(
            audit_mod.render(rows, title=f"Audit of {source.name} against {args.library}"),
            encoding="utf-8")
        print(f"written: {args.report}")

    # A wrong file is the finding; an entry with no file is a to-do. Exit 1 only
    # for the first, so CI can fail on "the library lies" and not on "the library
    # is incomplete".
    return 1 if counts.get("mismatch") else 0


def cmd_versions(args) -> int:
    """What *version* of each paper does the library actually hold?

    Identity and version are different questions. Four conditions prove the file
    is the paper you asked for; they say nothing about whether it is the
    preprint, the accepted manuscript, or the version of record. This command
    answers the second question, from the recorded provenance -- and, with
    `--backfill`, walks the files that predate the record.
    """
    lib = Library(args.library)
    if args.backfill:
        n = lib.record_provenance(lib.pdfs())
        print(f"backfilled {n} provenance lines\n")

    recs_all = provenance.load(lib.root)
    files = lib.pdfs()
    live = {str(p.relative_to(lib.root)) for p in files}
    # A record outlives a rename or a quarantine on purpose -- it is the audit
    # trail. But it must not be counted as a file that is there.
    recs = {k: v for k, v in recs_all.items() if k in live}
    stale = len(recs_all) - len(recs)
    counts = provenance.summarise(recs)
    untracked = [p for p in files if str(p.relative_to(lib.root)) not in recs]

    if args.json:
        _print({"counts": counts, "tracked": len(recs), "untracked": len(untracked),
                "files": [{"file": k, **{kk: vv for kk, vv in v.items()
                                         if kk in ("version", "version_basis", "url",
                                                   "tier", "transport", "retrieved")}}
                          for k, v in sorted(recs.items())]})
        return 0

    no_url = sum(1 for v in recs.values() if not v.get("url"))
    print(f"tracked {len(recs)} of {len(files)} files")
    for k in ("vor", "accepted", "preprint", "unknown"):
        print(f"  {k:<9} {counts.get(k, 0)}")
    if untracked:
        print(f"  (untracked {len(untracked)}: run --backfill)")
    if stale:
        print(f"  (stale {stale}: records whose file moved or was quarantined -- kept as audit trail)")
    if no_url:
        # Worth stating plainly: for a file already on disk there is often no way
        # to tell the version. Six publisher-looking PDFs sampled in this library
        # carried neither a DOI, nor a volume/page line, nor a copyright notice on
        # page 1 -- they were arXiv-style downloads of published papers. "unknown"
        # is the correct answer there, and it is why the URL is now recorded at
        # download time instead of reconstructed afterwards.
        print(f"  {no_url} records carry no download URL: their version was inferred from "
              f"page 1 only, and {counts.get('unknown', 0)} had no marker there either.")
        print("  New fetches record the URL and the transport, so this improves from here on.")

    risky = [(k, v) for k, v in recs.items() if v.get("version") == "preprint"]
    if risky and not args.quiet:
        print("\npreprints (a citation of a journal article must not rest on these):")
        for k, v in sorted(risky):
            print(f"  {Path(k).name[:66]:<66} {v.get('version_basis','')[:44]}")
    if args.verbose:
        print("\nall records:")
        for k, v in sorted(recs.items()):
            print(f"  {v.get('version','?'):<9} {Path(k).name[:60]:<60} "
                  f"{v.get('transport','-'):<12} {v.get('version_basis','')[:40]}")
    return 0


def _skill_text() -> str:
    """The packaged SKILL.md, read from the wheel rather than from the repo.

    Shipping it inside the wheel is the whole point: the CLI and the skill can no
    longer disagree, because there is only one file and one version number.
    """
    try:
        from importlib.resources import files
        return files("litfetch.skill").joinpath("SKILL.md").read_text(encoding="utf-8")
    except Exception:
        # Running from a source checkout without installed package data.
        here = Path(__file__).resolve().parent / "skill" / "SKILL.md"
        return here.read_text(encoding="utf-8")


def cmd_skill(args) -> int:
    text = _skill_text()
    raw = text.encode("utf-8")

    if args.print:
        sys.stdout.write(text)
        return 0

    dest = Path(args.target).expanduser() / args.name / "SKILL.md"
    if args.check:
        if not dest.exists():
            print(f"missing: {dest}")
            return 1
        same = dest.read_bytes() == raw
        print(("identical: " if same else "differs: ") + str(dest))
        return 0 if same else 1

    old = dest.read_bytes() if dest.exists() else b""
    if old == raw:
        print(f"up to date: {dest}")
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    if old:
        print(f"updated: {dest} ({len(old)} -> {len(raw)} bytes)")
    else:
        print(f"installed: {dest} ({len(raw)} bytes)")
    return 0


def cmd_schema(args) -> int:
    text = model.schema_json()
    if args.write:
        Path(args.write).write_text(text, encoding="utf-8")
        print(f"written: {args.write}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_quarantine(args) -> int:
    lib = Library(args.library)
    dest = lib.quarantine(args.file, reason=args.reason, what_it_is=args.what or "")
    print(f"moved to {dest}")
    return 0


def cmd_ledger(args) -> int:
    bib = []
    if args.bibliography:
        bib = ledger.parse_bibliography(Path(args.bibliography).read_text(encoding="utf-8"))
    skim = []
    for s in (args.skimmed or []):
        skim += [x for x in s.split(",") if x]
    overrides, skip = {}, ["logo"]
    if args.overrides:
        cfg = json.loads(Path(args.overrides).read_text(encoding="utf-8"))
        overrides = cfg.get("overrides", cfg if "overrides" not in cfg else {})
        skip = cfg.get("non_target", skip)
        skim += cfg.get("skimmed", [])
    rows = ledger.classify(args.library, bib, skim, overrides, skip)
    if args.json:
        print(ledger.to_json(rows))
    else:
        print(ledger.render(rows))
    if args.write:
        action = ledger.write_into(args.write, ledger.render_block(rows))
        print(f"{action}: {args.write}")
    return 0


def cmd_refs(args) -> int:
    """The registry is the file; the review's bibliography is a rendered view.

    find   -- before downloading anything, is this work already registered?
    add    -- register it (refuses on a match unless --force), then re-render.
    render -- regenerate the review's section from the list.
    check  -- the list and the review still agree?
    """
    list_path = Path(args.list)
    review = Path(args.review)

    def _load():
        blocks = refs_mod.load(list_path)
        if not blocks:
            print(f"no blocks in {list_path}", file=sys.stderr)
        return blocks

    if args.op == "find":
        blocks = _load()
        if not blocks:
            return 2
        hits = refs_mod.find(blocks, args.query, year=args.year or "",
                             surname=args.surname or "")
        if not hits:
            print("no match -- safe to download and register")
            return 0
        print(f"{len(hits)} possible match(es) -- do NOT download before reading these:")
        for m in hits[:8]:
            print(f"  {m.key}  score {m.score:<5} [{m.reason}]  ({m.year}) {m.title[:70]}")
        return 1

    if args.op == "add":
        blocks = _load()
        if not blocks:
            return 2
        first = args.authors.split(",")[0] if args.authors else ""
        hits = refs_mod.find(blocks, args.title, year=args.year or "", surname=first)
        if hits and not args.force:
            print("possible duplicate -- register nothing until you have read these:",
                  file=sys.stderr)
            for m in hits[:5]:
                print(f"  {m.key}  score {m.score:<5} [{m.reason}]  ({m.year}) {m.title[:70]}",
                      file=sys.stderr)
            print("(if they really are different papers, re-run with --force)", file=sys.stderr)
            return 1
        nums = [int(b.key[1:]) for b in blocks
                if b.kind == "entry" and b.key[1:].isdigit()]
        key = f"R{max(nums) + 1}"
        tail = f"（{args.note}）" if args.note else ""
        venue = f" {args.venue}" if args.venue else ""
        doi = f" DOI:{args.doi}" if args.doi else ""
        entry = f"{args.authors or '?'}. ({args.year or 'n.d.'}). {args.title}.{venue}{doi}{tail}"
        gi = len(blocks)
        if args.group:
            for k, b in enumerate(blocks):
                if b.kind == "heading" and args.group in b.text:
                    gi = k + 1
                    while (gi < len(blocks) and blocks[gi].kind != "heading"
                           and blocks[gi].group == b.group):
                        gi += 1
                    break
        sm = re.match(r"^\s*([^\W\d_][\w'\-]+)", args.authors or "", re.UNICODE)
        new = refs_mod.Block(kind="entry", key=key, text=entry,
                             title=refs_mod.title_of(entry),
                             year=args.year or "",
                             surname=refs_mod._fold(sm.group(1)) if sm else "",
                             doi=args.doi or "", group=args.group or "")
        blocks.insert(gi, new)
        fixed = []
        for b in blocks:
            if b.kind == "entry" and fixed and fixed[-1].kind == "entry":
                fixed.append(refs_mod.Block(kind="blank"))
            fixed.append(b)
        refs_mod.save(list_path, fixed)
        review.write_text(refs_mod.apply_render(review.read_text(encoding="utf-8"), fixed),
                          encoding="utf-8")
        print(f"registered {key}: {entry[:90]}")
        print(f"review re-rendered from {list_path}")
        return 0

    if args.op == "render":
        blocks = _load()
        if not blocks:
            return 2
        text = review.read_text(encoding="utf-8")
        if args.write:
            review.write_text(refs_mod.apply_render(text, blocks), encoding="utf-8")
            print(f"rewrote {review} from {list_path}")
        else:
            print(refs_mod.render(blocks))
        return 0

    blocks = _load()
    if not blocks:
        return 2
    problems = refs_mod.check(review.read_text(encoding="utf-8"), blocks)
    if not problems:
        n = sum(1 for b in blocks if b.kind == "entry")
        print(f"ok: {list_path.name} matches the review's generated section ({n} entries)")
        return 0
    for p in problems:
        print(f"PROBLEM: {p}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="litfetch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"litfetch {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    dg = sub.add_parser("diagnose", help="classify why a download fails, per candidate")
    dg.add_argument("--doi")
    dg.add_argument("--title")
    dg.add_argument("--year")
    dg.add_argument("--authors")
    dg.add_argument("--url", action="append")
    dg.add_argument("--no-arxiv", action="store_true")
    dg.add_argument("--timeout", type=float, default=20.0)
    dg.add_argument("--json", action="store_true")
    dg.set_defaults(func=cmd_diagnose)

    d = sub.add_parser("doctor", help="report what this machine supports")
    d.add_argument("--library")
    d.add_argument("--strict", action="store_true",
                   help="exit 2 on a degraded-but-working install too (missing "
                        "LITFETCH_MAILTO or curl_cffi), not only on a fatal one")
    d.set_defaults(func=cmd_doctor)

    v = sub.add_parser("verify", help="apply the four conditions to one file")
    v.add_argument("file")
    v.add_argument("--title", default=None)
    v.add_argument("--year", default=None)
    v.add_argument("--threshold", type=float, default=0.8)
    v.add_argument("--require-text", action="store_true")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_verify)

    dc = sub.add_parser("discover", help="find copies outside the OA indexes (author pages)")
    dc.add_argument("--title")
    dc.add_argument("--doi")
    dc.add_argument("--year")
    dc.add_argument("--authors")
    dc.add_argument("--num", default=None)
    dc.add_argument("--web", action="store_true",
                    help="also ask a search engine for \"title\" filetype:pdf")
    dc.add_argument("--rows", type=int, default=6)
    dc.add_argument("--no-arxiv", action="store_true")
    dc.add_argument("--timeout", type=float, default=20.0)
    dc.add_argument("--json", action="store_true")
    dc.set_defaults(func=cmd_discover)

    s = sub.add_parser("scan", help="duplicates, non-PDFs, totals")
    s.add_argument("library")
    s.add_argument("--json", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("resolve", help="list candidate sources in tier order")
    r.add_argument("--doi")
    r.add_argument("--title")
    r.add_argument("--year")
    r.add_argument("--authors")
    r.add_argument("--url", action="append")
    r.add_argument("--no-arxiv", action="store_true")
    r.add_argument("--rank", action="store_true",
                   help="show the scored title->DOI candidates")
    r.add_argument("--min-score", type=float, default=0.9,
                   help="title-match threshold for accepting a DOI (default 0.9)")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_resolve)

    f = sub.add_parser("fetch", help="download a reference list with verification")
    f.add_argument("refs")
    f.add_argument("--library", required=True)
    f.add_argument("--layer", default=None, help="sub-directory to store into")
    f.add_argument("--browser", action="store_true", help="enable the Chrome tier")
    f.add_argument("--threshold", type=float, default=0.8)
    f.add_argument("--only", default=None,
                   help="ref numbers: comma separated, ranges (L001-L020), or globs (COGR-L*)")
    f.add_argument("--dry-run", action="store_true")
    f.add_argument("--require-text", action="store_true")
    f.add_argument("--log", default=None)
    f.add_argument("--tiers", default=None,
                   help="restrict to tiers, e.g. L0 or L0,L3 (skips slow paywalled tiers)")
    f.add_argument("--timeout", type=float, default=60.0, help="per-request timeout, seconds")
    f.add_argument("--budget", type=float, default=None,
                   help="per-reference wall-clock budget, seconds (skip the rest of the tiers)")
    f.add_argument("-v", "--verbose", action="store_true", help="print each attempt")
    f.add_argument("--resume", action="store_true",
                   help="skip references already verified in --log")
    f.add_argument("--skip-existing", action="store_true",
                   help="skip references whose file is already in the library")
    f.add_argument("--no-tls", action="store_true",
                   help="disable the TLS-fingerprint retry (curl_cffi)")
    f.add_argument("--min-bytes", type=int, default=5000,
                   help="reject files smaller than this (default 5000)")
    f.add_argument("--version-required", default=None,
                   choices=["preprint", "accepted", "vor", "unknown"],
                   help="reject a file whose version is not the one asked for")
    f.add_argument("--json", action="store_true",
                   help="print the run report as JSON on stdout (human lines go to stderr)")
    f.set_defaults(func=cmd_fetch)

    cl = sub.add_parser("classify", help="plan how each PDF can be obtained; --fetch the auto-reachable")
    cl.add_argument("refs", help="refs.json (litfetch format) or a CID/link/title_hint CSV")
    cl.add_argument("--library", default=None, help="with --fetch: store into this library")
    cl.add_argument("--layer", default=None, help="sub-directory to store into")
    cl.add_argument("--fetch", action="store_true",
                    help="download the AUTO references into --library (verified)")
    cl.add_argument("--only", default=None,
                    help="ref numbers: comma separated, ranges (L001-L020), or globs (COGR-L*)")
    cl.add_argument("--known-broken", default="",
                    help="comma separated CIDs whose source PDF is corrupt (-> ALT_SOURCE)")
    cl.add_argument("--oa-lookup", action="store_true",
                    help="for subscription DOIs, query OpenAlex/Unpaywall/Crossref for "
                         "an OA preprint copy (network; off by default so classify stays static)")
    cl.add_argument("--mailto", default="",
                    help="contact email for the OA APIs (Unpaywall requires one)")
    cl.add_argument("--plan", default=None, help="write the verdict table to this CSV")
    cl.add_argument("--json", action="store_true")
    cl.add_argument("--verify", dest="verify", action="store_true", default=True,
                    help="verify downloaded title block (auto-skipped for short CSV hints)")
    cl.add_argument("--no-verify", dest="verify", action="store_false")
    cl.add_argument("--force", action="store_true",
                    help="store even if title-block verification fails")
    cl.add_argument("--timeout", type=float, default=25.0, help="per-request timeout, seconds")
    cl.add_argument("--threshold", type=float, default=0.8)
    cl.set_defaults(func=cmd_classify)

    im = sub.add_parser("import", help="ingest a manually downloaded PDF (verified)")
    im.add_argument("file", nargs="?", default=None)
    im.add_argument("--from-dir", default=None,
                    help="match every PDF in this folder against --refs and import")
    im.add_argument("--refs", default=None, help="refs.json for --from-dir")
    im.add_argument("--library", required=True)
    im.add_argument("--title", default=None)
    im.add_argument("--year", default=None)
    im.add_argument("--authors", default=None)
    im.add_argument("--layer", default=None)
    im.add_argument("--threshold", type=float, default=0.8)
    im.add_argument("--copy", action="store_true", help="copy instead of move")
    im.add_argument("--source-url", default="",
                    help="where the file came from (recorded in provenance)")
    im.add_argument("--force", action="store_true",
                    help="accept a failing file into _misfiled/ (recorded)")
    im.set_defaults(func=cmd_import)

    au = sub.add_parser("audit", help="entry-by-entry: is there a file, and is it the right paper")
    au.add_argument("--library", required=True)
    src = au.add_mutually_exclusive_group()
    src.add_argument("--bibliography", help="a review.md with **Rnn.** bibliography lines")
    src.add_argument("--zotero", help="CSL-JSON (.json) or BibTeX (.bib) exported from Zotero")
    au.add_argument("--threshold", type=float, default=0.8)
    au.add_argument("--limit", type=int, default=None, help="audit only the first N files")
    au.add_argument("--skip-unverifiable", action="store_true")
    au.add_argument("--report", default=None, help="write the markdown report here")
    au.add_argument("--json", action="store_true")
    au.set_defaults(func=cmd_audit)

    ver = sub.add_parser("versions", help="preprint / accepted / version-of-record per file")
    ver.add_argument("--library", required=True)
    ver.add_argument("--backfill", action="store_true",
                     help="write provenance lines for files that have none yet")
    ver.add_argument("--quiet", action="store_true", help="counts only")
    ver.add_argument("-v", "--verbose", action="store_true")
    ver.add_argument("--json", action="store_true")
    ver.set_defaults(func=cmd_versions)

    sk = sub.add_parser("skill", help="install or check the bundled agent skill")
    sk.add_argument("--target", default="~/.workbuddy/skills",
                    help="skills directory (default: ~/.workbuddy/skills)")
    sk.add_argument("--name", default="lit-fetch", help="skill folder name")
    sk.add_argument("--check", action="store_true",
                    help="compare with the packaged copy; exit 1 if they differ")
    sk.add_argument("--print", dest="print", action="store_true",
                    help="write the skill to stdout")
    sk.set_defaults(func=cmd_skill)

    sc = sub.add_parser("schema", help="print the evidence-record JSON Schema")
    sc.add_argument("--write", default=None)
    sc.set_defaults(func=cmd_schema)

    q = sub.add_parser("quarantine", help="move a wrong file into _misfiled/")
    q.add_argument("file")
    q.add_argument("--library", required=True)
    q.add_argument("--reason", required=True)
    q.add_argument("--what", default="")
    q.set_defaults(func=cmd_quarantine)

    l = sub.add_parser("ledger", help="classify the library into A/B/C/X")
    l.add_argument("--library", required=True)
    l.add_argument("--bibliography", default=None)
    l.add_argument("--skimmed", action="append", help="file-name fragments at level B")
    l.add_argument("--overrides", default=None,
                   help="JSON: {file-name fragment: evidence} for non-mechanical matches")
    l.add_argument("--json", action="store_true")
    l.add_argument("--write", default=None)
    l.set_defaults(func=cmd_ledger)
    rf = sub.add_parser("refs", help="the key registry: find / add / render / check")
    rf.add_argument("op", choices=["find", "add", "render", "check"])
    rf.add_argument("--list", default="literature/REFS.jsonl")
    rf.add_argument("--review", default="explainable_world_model_litreview/从预测世界到解释世界_文献综述_V3.md",
                    help="the document whose bibliography is rendered from --list")
    rf.add_argument("query", nargs="?", default="", help="find: title fragment or DOI")
    rf.add_argument("--title", help="add: the work's title")
    rf.add_argument("--year", default="")
    rf.add_argument("--authors", default="", help='add: "Surname, X., and Y, Z."')
    rf.add_argument("--venue", default="")
    rf.add_argument("--doi", default="")
    rf.add_argument("--note", default="", help="add: the judgment note, stored verbatim")
    rf.add_argument("--group", default="", help="add: which batch heading this belongs under")
    rf.add_argument("--surname", default="")
    rf.add_argument("--write", action="store_true", help="render: rewrite the review in place")
    rf.add_argument("--force", action="store_true", help="add: register even though find matched")
    rf.set_defaults(func=cmd_refs)
    return ap


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
