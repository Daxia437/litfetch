import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litfetch.core import ledger  # noqa: E402

BIB = """
**R141.** Michel, F., and Siegle, M. (2024). Formal error bounds.
**R9.** Schölkopf, B., Locatello, F. (2021). Toward causal representation learning.
**R149.** Khemakhem, I. (2020). Variational autoencoders and nonlinear ICA.
"""


def touch_pdf(path: Path, payload: bytes = b"%PDF-1.4\n" + b"0" * 6000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def test_parse_bibliography_reads_number_surname_year():
    entries = ledger.parse_bibliography(BIB)
    assert [e.num for e in entries] == [141, 9, 149]
    assert entries[1].surname == "scholkopf"      # accents folded
    assert entries[1].year == "2021"


def test_classify_a_c_and_duplicates(tmp_path):
    touch_pdf(tmp_path / "L1" / "Scholkopf_2021_Causal_Representation.pdf")
    touch_pdf(tmp_path / "L1" / "Kipf_2018_NRI.pdf")
    touch_pdf(tmp_path / "L2" / "copy_of_scholkopf.pdf",
              (tmp_path / "L1" / "Scholkopf_2021_Causal_Representation.pdf").read_bytes())

    rows = ledger.classify(tmp_path, ledger.parse_bibliography(BIB))
    by_name = {r.path.as_posix(): r for r in rows}
    assert by_name["L1/Scholkopf_2021_Causal_Representation.pdf"].status == "A"
    assert by_name["L1/Kipf_2018_NRI.pdf"].status == "C"
    assert by_name["L2/copy_of_scholkopf.pdf"].duplicate_of is not None


def test_summary_counts_distinct_not_files(tmp_path):
    touch_pdf(tmp_path / "L1" / "a.pdf")
    touch_pdf(tmp_path / "L2" / "b.pdf", (tmp_path / "L1" / "a.pdf").read_bytes())
    rows = ledger.classify(tmp_path)
    s = ledger.summary(rows)
    assert s["files"] == 2 and s["distinct"] == 1 and s["duplicate_copies"] == 1
    assert s["c"] == 2 and s["unread"]


def test_render_has_layers_and_unread_list(tmp_path):
    touch_pdf(tmp_path / "L1" / "x.pdf")
    text = ledger.render(ledger.classify(tmp_path))
    assert "| layer | A | B | C | X |" in text
    assert "Unread (C):" in text


def test_render_block_is_marked_and_counts_add_up(tmp_path):
    touch_pdf(tmp_path / "L1" / "a.pdf")
    block = ledger.render_block(ledger.classify(tmp_path))
    assert block.startswith(ledger.BEGIN) and block.rstrip().endswith(ledger.END)
    assert "| PDF files on disk | **1** |" in block


def test_write_into_replaces_only_the_generated_region(tmp_path):
    doc = tmp_path / "STATUS.md"
    doc.write_text("prose above\n\n" + ledger.BEGIN + "\nold\n" + ledger.END + "\n\nprose below\n",
                   encoding="utf-8")
    touch_pdf(tmp_path / "lib" / "L1" / "a.pdf")
    block = ledger.render_block(ledger.classify(tmp_path / "lib"))
    action = ledger.write_into(doc, block)
    text = doc.read_text(encoding="utf-8")
    assert action == "replaced"
    assert "prose above" in text and "prose below" in text
    assert "old" not in text
    assert text.count(ledger.BEGIN) == 1


def test_write_into_appends_when_unmarked(tmp_path):
    doc = tmp_path / "STATUS.md"
    doc.write_text("hand written notes\n", encoding="utf-8")
    touch_pdf(tmp_path / "lib" / "L1" / "a.pdf")
    action = ledger.write_into(doc, ledger.render_block(ledger.classify(tmp_path / "lib")))
    assert action == "appended"
    assert "hand written notes" in doc.read_text(encoding="utf-8")
