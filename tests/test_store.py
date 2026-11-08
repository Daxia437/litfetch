import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litfetch.core.store import Library, make_name, safe_stem  # noqa: E402


def test_safe_stem_removes_windows_illegal_characters():
    assert ":" not in safe_stem("A: B? C*")
    assert not safe_stem("trailing. ").endswith((" ", "."))


def test_safe_stem_handles_reserved_device_names():
    assert safe_stem("CON") == "CON_doc"
    assert safe_stem("lpt3") == "lpt3_doc"


def test_safe_stem_keeps_cjk_and_caps_length():
    s = safe_stem("科学表征" * 40, limit=60)
    assert len(s) <= 60
    assert "科学表征" in s


def test_make_name_shape():
    n = make_name("Esponda", 2016, "Berk-Nash Equilibrium: A Framework")
    assert n.startswith("Esponda_2016_")
    assert n.endswith(".pdf")
    assert ":" not in n


def test_add_file_deduplicates_by_content(tmp_path):
    lib = Library(tmp_path / "lib")          # sources live outside the library
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    a, b = inbox / "a.pdf", inbox / "b.pdf"
    payload = b"%PDF-1.4\n" + b"0" * 6000
    a.write_bytes(payload)
    b.write_bytes(payload)

    first = lib.add_file(a, author="Esponda", year=2016, title="Berk Nash")
    second = lib.add_file(b, author="Esponda", year=2016, title="Berk Nash")
    assert first == second
    assert b.exists()          # the duplicate source is left in place
    assert len(list(lib.root.rglob("*.pdf"))) == 1


def test_same_name_collision_gets_a_suffix(tmp_path):
    lib = Library(tmp_path / "lib")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for i, payload in enumerate((b"%PDF-1.4\n" + b"1" * 6000,
                                 b"%PDF-1.4\n" + b"2" * 6000)):
        p = inbox / f"src{i}.pdf"
        p.write_bytes(payload)
        lib.add_file(p, author="Doe", year=2020, title="Same")
    names = sorted(x.name for x in lib.root.glob("Doe_*.pdf"))
    assert names == ["Doe_2020_Same.pdf", "Doe_2020_Same_2.pdf"]


def test_quarantine_moves_sibling_and_writes_manifest(tmp_path):
    lib = Library(tmp_path / "lib")
    bad = lib.root / "L13" / "Kipf_2018_NRI.pdf"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"%PDF-1.4\n" + b"x" * 6000)
    bad.with_suffix(".pdf.txt").write_text("actually another paper", encoding="utf-8")

    dest = lib.quarantine(bad, reason="identity mismatch", what_it_is="Online Relational Inference")
    assert dest.exists()
    assert not bad.exists()
    manifest = lib.root / "_misfiled" / "MANIFEST.jsonl"
    rows = [json.loads(l) for l in manifest.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["reason"] == "identity mismatch"
    assert any(f.endswith(".txt") for f in rows[-1]["moved"])


def test_duplicates_and_pdfs_skip_misfiled(tmp_path):
    lib = Library(tmp_path / "lib")
    (lib.root / "a").mkdir(parents=True)
    (lib.root / "a" / "x.pdf").write_bytes(b"%PDF-1.4\n" + b"z" * 6000)
    (lib.root / "_misfiled").mkdir()
    (lib.root / "_misfiled" / "y.pdf").write_bytes(b"%PDF-1.4\n" + b"z" * 6000)
    assert [p.name for p in lib.pdfs()] == ["x.pdf"]
