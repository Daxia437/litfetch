"""The audit must still say "wrong file" when the file is wrong.

The rules were loosened three times while running this against the real library
(cover sheets, spelling variants, kerning). Each loosening is defensible on its
own; together they invite the failure mode this project exists to catch -- a
classifier tuned until nothing is flagged. So the fixture contains a *known*
wrong pair, and it must keep coming out as `mismatch`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf  # noqa: E402

from litfetch.core import audit  # noqa: E402
from litfetch.core.store import Library  # noqa: E402

GOOD = "Formal Error Bounds for the State Space Reduction of Markov Chains"
OTHER = "Online Relational Inference for Evolving Multi-agent Interacting Systems"


def _pdf(path: Path, text: str, year: str = "2024") -> Path:
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 520, 760),
                                  f"{text}\nMichel, Siegle. {year}.", fontsize=9)
    for _ in range(2):
        doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 520, 760),
                                      "continuation " * 20, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def _entries(text: str):
    return audit.entries_from_markdown(text)


BIB = """**R1.** Michel, F. and Siegle, M. (2024). Formal Error Bounds for the State Space Reduction of Markov Chains. *Performance Evaluation*, 167: 102464.
**R2.** Kipf, T. (2018). Neural Relational Inference for Interacting Systems. *ICML*.
"""


def test_a_wrong_file_is_still_a_mismatch(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.root.mkdir(parents=True)
    _pdf(lib.root / "Michel_2024_Formal_Error_Bounds.pdf", GOOD)
    _pdf(lib.root / "Kipf_2018_Neural_Relational_Inference.pdf", OTHER, "2018")

    rows = audit.audit(lib, _entries(BIB), threshold=0.8, min_bytes=1000)
    by_key = {r.key: r for r in rows}
    assert by_key["R1"].verdict == "verified"
    assert by_key["R2"].verdict == "mismatch", by_key["R2"].detail


def test_an_exact_title_with_another_year_is_other_version(tmp_path):
    """The R143 case: right paper, wrong year on the page."""
    lib = Library(tmp_path / "lib")
    lib.root.mkdir(parents=True)
    _pdf(lib.root / "Michel_2024_Formal_Error_Bounds.pdf", GOOD, year="1988")

    rows = audit.audit(lib, _entries(BIB), threshold=0.8, min_bytes=1000)
    r = {x.key: x for x in rows}["R1"]
    assert r.verdict == "other-version", r.detail


def test_missing_and_orphan_are_both_reported(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.root.mkdir(parents=True)
    _pdf(lib.root / "Michel_2024_Formal_Error_Bounds.pdf", GOOD)
    _pdf(lib.root / "Nobody_1999_Unrelated.pdf", "A Paper Nobody Cited", "1999")

    rows = audit.audit(lib, _entries(BIB), threshold=0.8, min_bytes=1000)
    counts = audit.summarise(rows)
    assert counts["missing"] == 1          # R2 has no file
    assert counts["orphan"] == 1           # Nobody 1999 has no entry


def test_a_scanned_file_is_unverifiable_not_a_mismatch(tmp_path):
    """No text layer means the identity was not checked -- say so, do not accuse."""
    lib = Library(tmp_path / "lib")
    lib.root.mkdir(parents=True)
    doc = pymupdf.open()
    doc.new_page()                          # no text at all
    doc.save(str(lib.root / "Michel_2024_Formal_Error_Bounds.pdf"))
    doc.close()

    rows = audit.audit(lib, _entries(BIB), threshold=0.8, min_bytes=1000)
    r = {x.key: x for x in rows}["R1"]
    assert r.verdict == "unverifiable", r.detail


def test_an_entry_without_a_title_is_not_guessed(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.root.mkdir(parents=True)
    _pdf(lib.root / "Michel_2024_Formal_Error_Bounds.pdf", GOOD)

    bib = "**R1.** Michel, F. (2024). arXiv:2403.07618（会议版待补）\n"
    rows = audit.audit(lib, _entries(bib), threshold=0.8, min_bytes=1000)
    assert rows[0].verdict == "unverifiable"


def test_csl_json_is_accepted_as_an_input():
    import json

    csl = json.dumps([{
        "id": "ITEM1", "type": "article-journal", "title": GOOD,
        "author": [{"family": "Michel", "given": "F."}],
        "issued": {"date-parts": [[2024]]}, "DOI": "10.1/x",
    }])
    entries = audit.entries_from_csl_json(csl)
    assert entries[0].key == "ITEM1"
    assert entries[0].surname == "Michel"
    assert entries[0].year == "2024"
    assert entries[0].title == GOOD


def test_bibtex_is_read_without_a_full_parser():
    bib = """
@article{michel2024,
  title = {Formal Error Bounds for the State Space Reduction of Markov Chains},
  author = {Michel, F. and Siegle, M.},
  year = {2024},
  doi = {10.1016/j.peva.2024.102464}
}
"""
    entries = audit.entries_from_bibtex(bib)
    assert len(entries) == 1
    assert entries[0].surname == "Michel"
    assert entries[0].title.startswith("Formal Error Bounds")


ALIAS_BIB = """**R1.** Michel, F. and Siegle, M. (2024). Formal Error Bounds for the State Space Reduction of Markov Chains. *Performance Evaluation*.
**R2.** Liang, Y. (2025). VisualPredicator: Learning Abstract World Models with Neuro-Symbolic Predicates. *ICLR*.
**R3.** Rainforth, T. (2024). Modern Bayesian Experimental Design. *Statistical Science*.
**R4.** Rainforth, T. (2024). Modern Bayesian experimental design. *Statistical Science*.
**R5.** Hoel, E. (2026). Quantifying emergent complexity. *Patterns*.
"""


def test_the_alias_table_settles_what_a_file_name_cannot(tmp_path):
    """Three entries the naming rule cannot pair, and one that is not about a file.

    R2 is code-named (no surname in the name), R4 is the same paper as R3 under a
    second key, R5 was only ever obtainable as text. All three are `missing`
    without the table and answered with it -- but the table must not be able to
    wave anything through: a row without `why` is refused.
    """
    lib = Library(tmp_path / "lib")
    lib.root.mkdir(parents=True)
    _pdf(lib.root / "Michel_2024_Formal_Error_Bounds.pdf", GOOD)
    _pdf(lib.root / "VisualPredicator_ICLR2025.pdf",
         "VisualPredicator Learning Abstract World Models with Neuro-Symbolic Predicates",
         "2025")
    _pdf(lib.root / "Rainforth_2024_Modern_Bayesian_Experimental_Design.pdf",
         "Modern Bayesian Experimental Design Rainforth, Foster, Ivanova, Bickford Smith")
    (lib.root / "hoel2026.txt").write_text("full text held as text", encoding="utf-8")

    entries = _entries(ALIAS_BIB)
    bare = {r.key: r.verdict for r in audit.audit(lib, entries, min_bytes=1000)}
    assert bare["R2"] == "missing"          # the name carries no surname
    assert bare["R4"] == "missing"          # second key, no second file

    alias_path = lib.root / "aliases.json"
    alias_path.write_text("""{"entries": {
        "R2": {"file": "VisualPredicator_ICLR2025.pdf", "why": "code-named system"},
        "R4": {"duplicate_of": "R3", "why": "same Rainforth 2024 paper"},
        "R5": {"full_text": "hoel2026.txt", "why": "publisher PDF restricted"}
    }}""", encoding="utf-8")

    table, problems = audit.load_aliases(lib.root)
    assert not problems
    rows = audit.audit(lib, entries, aliases=table, min_bytes=1000)
    got = {r.key: r.verdict for r in rows}
    assert got["R2"] == "verified"
    assert got["R3"] == "verified"
    assert got["R4"] == "duplicate"
    assert got["R5"] == "full-text-only"
    assert "missing" not in got.values()


def test_an_alias_without_a_reason_is_refused(tmp_path):
    """An unexplained exception is how a ledger rots."""
    root = tmp_path / "lib"
    root.mkdir()
    (root / "aliases.json").write_text(
        '{"entries": {"R1": {"file": "x.pdf"}}}', encoding="utf-8")
    table, problems = audit.load_aliases(root)
    assert table == {}
    assert problems and "why" in problems[0]
