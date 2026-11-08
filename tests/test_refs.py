"""The registry file and the review's generated section must never drift.

Three properties matter, and each one exists because its absence already cost a
day: the round trip is exact (a view that loses a blank line is a lie); `find`
catches a work already registered (the cheapest duplicate is the one never
fetched); and `check` fails when the review's section and the list disagree
(hand-copied second lists always drift).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litfetch.core import refs  # noqa: E402

REVIEW = """# review

## one

Some body text.

## 参考文献

> a person's note, outside the markers

<!-- BEGIN GENERATED:refs -->
**L0 基础**

**R1.** Michel, F. and Siegle, M. (2024). Formal Error Bounds for the State Space Reduction of Markov Chains. *Performance Evaluation*.

**R2.** Kipf, T. (2018). Neural Relational Inference for Interacting Systems. *ICML*.
<!-- END GENERATED:refs -->

## 附录 A

appendix text
"""


def _write(review: Path, entries: list) -> Path:
    list_path = review.parent / "REFS.jsonl"
    blocks = refs.parse_section(review)
    for b in blocks:
        if b.kind == "entry":
            for k, v in {"R1": {"title": "Formal Error Bounds for the State Space Reduction of Markov Chains",
                                "year": "2024", "surname": "michel"},
                         "R2": {"title": "Neural Relational Inference for Interacting Systems",
                                "year": "2018", "surname": "kipf"}}.get(b.key, {}).items():
                setattr(b, k, v)
    refs.save(list_path, blocks)
    review.write_text(refs.apply_render(review, blocks), encoding="utf-8")
    return list_path


def test_round_trip_is_exact(tmp_path):
    """A view that loses a blank line is a lie; render must reproduce the section."""
    p = tmp_path / "review.md"
    p.write_text(REVIEW, encoding="utf-8")
    blocks = refs.parse_section(REVIEW)
    inside = REVIEW.split(refs.BEGIN, 1)[1].split(refs.END, 1)[0].strip("\n")
    assert refs.render(blocks) == inside


def test_find_hits_before_a_download(tmp_path):
    p = tmp_path / "review.md"
    p.write_text(REVIEW, encoding="utf-8")
    list_path = _write(p, [])
    blocks = refs.load(list_path)

    hit = refs.find(blocks, "Formal Error Bounds for the State Space Reduction of Markov Chains")
    assert hit and hit[0].key == "R1"

    doi = refs.find(blocks, "whatever 10.1234/abcd.2024")
    assert doi == []            # no DOI recorded, no DOI claimed

    miss = refs.find(blocks, "A Fourier approach to nonparametric deconvolution")
    assert miss == []


def test_add_refuses_a_duplicate_and_then_registers(tmp_path):
    """The gate is the point: the same paper must not get a second key."""
    from litfetch.cli import cmd_refs
    import argparse

    p = tmp_path / "review.md"
    p.write_text(REVIEW, encoding="utf-8")
    list_path = _write(p, [])
    before = len([b for b in refs.load(list_path) if b.kind == "entry"])

    a = argparse.Namespace(op="add", list=str(list_path), review=str(p),
                           title="Formal Error Bounds for the State Space Reduction of Markov Chains",
                           year="2024", authors="Someone, Else.", venue="", doi="",
                           note="", group="", force=False)
    assert cmd_refs(a) == 1                       # refused
    assert len([b for b in refs.load(list_path) if b.kind == "entry"]) == before

    a.force = True
    a.title = "A genuinely different paper on coarse graining"
    a.year = "2026"
    assert cmd_refs(a) == 0
    blocks = refs.load(list_path)
    keys = [b.key for b in blocks if b.kind == "entry"]
    assert "R3" in keys                           # next key, not a reused gap
    assert refs.check(p.read_text(encoding="utf-8"), blocks) == []


def test_check_catches_a_hand_edit(tmp_path):
    """Someone edits the review directly -- the next check must say so."""
    p = tmp_path / "review.md"
    p.write_text(REVIEW, encoding="utf-8")
    list_path = _write(p, [])
    edited = p.read_text(encoding="utf-8").replace("Kipf, T. (2018).", "Kipf, T. (2019).")
    p.write_text(edited, encoding="utf-8")
    problems = refs.check(p.read_text(encoding="utf-8"), refs.load(list_path))
    assert problems and "differs" in problems[0]
