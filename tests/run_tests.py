"""Dependency-free test runner.

`python tests/run_tests.py` works on any machine with Python 3.9+, which is the
point: the package is meant to be installable on three operating systems without
dragging a test framework along. (pytest also works, if you happen to have it.)

Fixtures are provided by name: `tmp_path` gets a fresh directory and is removed
afterwards; `monkeypatch` is a minimal stand-in for pytest's (setattr only, all
undone when the test returns); a module needing a missing optional dependency is
skipped.
"""

from __future__ import annotations

import importlib.util
import inspect
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_MISSING = object()


class MonkeyPatch:
    """The two methods the suite uses, with the same undo semantics as pytest."""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        old = getattr(target, name, _MISSING)
        self._undo.append((target, name, old))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            if old is _MISSING:
                try:
                    delattr(target, name)
                except AttributeError:
                    pass
            else:
                setattr(target, name, old)
        self._undo.clear()


MODULES = ["test_verify", "test_store", "test_ledger", "test_diagnose",
           "test_provenance", "test_sources", "test_core", "test_audit", "test_model", "test_skill",
           "test_fetch", "test_refs", "test_reachability", "test_report"]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    passed = failed = skipped = 0
    failures = []
    for modname in MODULES:
        try:
            mod = load(modname)
        except ImportError as exc:
            print(f"SKIP {modname}: {exc}")
            skipped += 1
            continue
        for name, fn in sorted(inspect.getmembers(mod, inspect.isfunction)):
            if not name.startswith("test_"):
                continue
            kwargs = {}
            params = inspect.signature(fn).parameters
            tmp = None
            mp = None
            if "tmp_path" in params:
                tmp = Path(tempfile.mkdtemp(prefix="litfetch-test-"))
                kwargs["tmp_path"] = tmp
            if "monkeypatch" in params:
                mp = MonkeyPatch()
                kwargs["monkeypatch"] = mp
            try:
                fn(**kwargs)
                passed += 1
                print(f"ok   {modname}.{name}")
            except Exception:
                failed += 1
                failures.append((modname, name, traceback.format_exc()))
                print(f"FAIL {modname}.{name}")
            finally:
                if mp is not None:
                    mp.undo()
                if tmp is not None:
                    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed, {skipped} module(s) skipped")
    for modname, name, tb in failures:
        print(f"\n--- {modname}.{name}\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
