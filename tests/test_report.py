"""Selection and run-report behaviour.

Both exist for the same reason: a batch run must not be able to fail quietly.
`select` makes a typo in `--only` loud; `report` turns twenty failure lines into
one histogram with the next action attached.
"""

import argparse
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litfetch import cli  # noqa: E402
from litfetch.core import report as report_mod  # noqa: E402
from litfetch.core.select import Selector, select, suggest  # noqa: E402
from litfetch.io import sources  # noqa: E402
from litfetch.io.fetch import Outcome  # noqa: E402


def _refs(*nums):
    return [sources.Reference(num=n, title=f"Paper {n}", year="2020") for n in nums]


# ---------------------------------------------------------------- selection

def test_empty_selector_keeps_everything():
    kept, sel, unmatched = select(_refs("L001", "L002"), None)
    assert len(kept) == 2 and not sel and unmatched == []


def test_range_is_inclusive():
    kept, _, _ = select(_refs("L001", "L002", "L003", "L004"), "L002-L003")
    assert [r.num for r in kept] == ["L002", "L003"]


def test_hyphen_inside_an_id_is_not_a_range():
    """COGR-L07 must not be read as "COGR".."L07"."""
    sel = Selector("COGR-L07")
    assert sel.match("COGR-L07")
    assert not sel.match("COGR-L70")
    assert sel.ranges == []


def test_glob_and_case_insensitivity():
    kept, _, _ = select(_refs("COGR-L01", "COGR-L02", "L001"), "cogr-l*")
    assert [r.num for r in kept] == ["COGR-L01", "COGR-L02"]
    assert Selector("l007").match("L007")


def test_unmatched_term_is_named_with_a_suggestion():
    nums = ["L007", "L010", "L0199"]
    _, _, unmatched = select(_refs(*nums), "L01,L007")
    assert unmatched == ["L01"]
    assert suggest("L01", nums)[0] == "L010"


def test_suggestion_prefers_the_same_number():
    """`L07` and `L007` are the same id to a human, whatever the padding."""
    assert suggest("L07", ["L070", "L007", "L071"])[0] == "L007"


def test_only_typo_exits_2_instead_of_running_empty(tmp_path):
    refs = tmp_path / "refs.json"
    refs.write_text(json.dumps([{"num": "L007", "title": "A"}]), encoding="utf-8")
    ns = argparse.Namespace(refs=str(refs), only="L01", library=str(tmp_path / "lib"))
    err = io.StringIO()
    with redirect_stderr(err):
        code = cli.cmd_fetch(ns)
    assert code == 2, "an empty selector must not look like a completed run"
    assert "matched none" in err.getvalue()


# ------------------------------------------------------------------- report

def _outcome(num, status, note="", blocked="", level="L1", version=""):
    return Outcome(ref=sources.Reference(num=num, title=f"Paper {num}"),
                   status=status, note=note, blocked=blocked, level=level,
                   version=version)


def test_reason_keys_cover_the_diagnosis_codes():
    assert report_mod.reason_of(_outcome("1", "blocked", "cloudflare: HTTP 403",
                                         blocked="cloudflare")) == "cloudflare"
    assert report_mod.reason_of(_outcome("2", "unavailable",
                                         "not_pdf: HTML page (78 bytes)")) == "not_pdf"
    assert report_mod.reason_of(_outcome("3", "unavailable",
                                         "no candidate URL found (and the browser "
                                         "tier was not enabled)")) == "no_url"
    assert report_mod.reason_of(_outcome("4", "unavailable",
                                         "budget of 60s exhausted")) == "budget"
    assert report_mod.reason_of(_outcome("5", "rejected",
                                         "title not found in the page-1 block")) == "identity"
    assert report_mod.reason_of(_outcome("6", "rejected",
                                         "version is preprint (page 1 says arXiv); "
                                         "required vor")) == "version"
    assert report_mod.reason_of(_outcome("7", "stored")) == ""


def test_histogram_and_gaps_exclude_finished_work():
    outcomes = [
        _outcome("1", "stored", version="vor"),
        _outcome("2", "duplicate"),
        _outcome("3", "skipped", "verified in an earlier run (--resume)"),
        _outcome("4", "blocked", "cloudflare: HTTP 403", blocked="cloudflare"),
        _outcome("5", "blocked", "challenge: HTTP 403", blocked="challenge"),
        _outcome("6", "unavailable", "not_pdf: HTML page (78 bytes)"),
    ]
    rep = report_mod.build_report(outcomes, library="L", skipped_tiers=[
        {"tier": "L1", "why": "no mailto", "fix": "export LITFETCH_MAILTO=you@example.org"}])
    assert rep["refs"] == 6 and rep["stored"] == 1
    assert rep["gaps"] == 3
    assert {r["reason"]: r["count"] for r in rep["reasons"]} == {
        "cloudflare": 1, "challenge": 1, "not_pdf": 1}
    assert rep["versions"] == {"vor": 1}
    text = report_mod.format_report(rep)
    assert "why 3 of 6 were not stored" in text
    assert "open in your own browser" in text          # the action is printed
    assert "not attempted" in text                     # skipped tier is named
    assert "LITFETCH_MAILTO" in text


def test_no_attempted_section_when_nothing_was_skipped():
    rep = report_mod.build_report([_outcome("1", "stored")])
    text = report_mod.format_report(rep)
    assert "not attempted" not in text
    assert "why" not in text


def test_fetch_json_keeps_stdout_parseable(tmp_path, monkeypatch):
    refs = tmp_path / "refs.json"
    refs.write_text(json.dumps([{"num": "L007", "title": "A", "year": "2020"}]),
                    encoding="utf-8")

    def fake_fetch(ref, *a, **kw):
        return _outcome("L007", "blocked", "cloudflare: HTTP 403", blocked="cloudflare")

    monkeypatch.setattr(cli, "fetch_ref", fake_fetch)
    ns = argparse.Namespace(
        refs=str(refs), only=None, library=str(tmp_path / "lib"), resume=False,
        log=None, json=True, layer=None, browser=False, threshold=0.8,
        dry_run=False, require_text=False, tiers=None, timeout=5.0, budget=None,
        verbose=False, no_tls=True, version_required=None, skip_existing=False,
        min_bytes=5000,
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.cmd_fetch(ns)
    assert code == 1
    payload = json.loads(out.getvalue())          # stdout is the report, alone
    assert payload["reasons"][0]["reason"] == "cloudflare"
    assert payload["gaps"] == 1
    assert "why 1 of 1 were not stored" in err.getvalue()
