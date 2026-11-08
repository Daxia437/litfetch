"""`core` must stay pure: no network, no optional dependency, no `io` import.

This is the boundary the whole split exists for, so it is checked statically.
The CI job that installs no extras is the runtime version of the same test.
"""

import importlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CORE_MODULES = ("verify", "provenance", "store", "ledger", "model")
FORBIDDEN_AT_MODULE_LEVEL = ("import pymupdf", "from pymupdf", "import fitz",
                             "import curl_cffi", "from curl_cffi",
                             "import websocket", "import urllib")


def test_every_core_module_imports():
    for name in CORE_MODULES:
        importlib.import_module(f"litfetch.core.{name}")


def test_core_does_not_import_optional_packages_at_module_level():
    for name in CORE_MODULES:
        mod = importlib.import_module(f"litfetch.core.{name}")
        for line in inspect.getsource(mod).splitlines():
            if line.startswith((" ", "\t")):
                continue          # inside a function: lazy import, which is allowed
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for bad in FORBIDDEN_AT_MODULE_LEVEL:
                assert not stripped.startswith(bad), f"{name}: {stripped}"


def test_importing_core_pulls_in_neither_io_nor_a_third_party_package():
    """Run it in a fresh interpreter: `sys.modules` is shared across the suite,
    so an in-process assertion would pass or fail depending on test order."""
    code = (
        "import json, sys, litfetch.core.store, litfetch.core.model\n"
        "from litfetch import verify_pdf\n"
        "print(json.dumps({\n"
        "  'io': [m for m in sys.modules if m.startswith('litfetch.io')],\n"
        "  'third': [m for m in ('pymupdf', 'fitz', 'curl_cffi', 'websocket')\n"
        "            if m in sys.modules]}))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env)
    assert out.returncode == 0, out.stderr
    loaded = json.loads(out.stdout)
    assert loaded["io"] == [], f"core pulled in {loaded['io']}"
    assert loaded["third"] == [], f"core pulled in {loaded['third']}"
