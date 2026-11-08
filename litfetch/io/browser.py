"""Tier L3: fetch the bytes from a real Chrome session over CDP.

Why this exists: on a campus network the paywall is usually *not* the blocker --
bot detection is. A scripted GET to Wiley answers 403, to IEEE 502, to Project
Euclid a 1 KB challenge page, while the same URL opens fine in a browser that
runs the JS and carries the session. So the answer is to drive the browser that
is already entitled, and to intercept the PDF response it receives.

Four pitfalls are wired in rather than documented:
  1. environment proxies hijack 127.0.0.1 -- proxy variables are stripped for
     the child process and NO_PROXY is set;
  2. Chrome inside a sandbox needs `--no-sandbox`, and a WebSocket client that
     sends an Origin header needs `--remote-allow-origins=*`;
  3. CDP is asynchronous: responses are matched by id, events are handled as
     they arrive, and the client never blocks on a send;
  4. landing pages rarely auto-load the PDF. After `Page.loadEventFired` the
     client looks for a PDF link in the DOM and follows it once.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

CHROME_CANDIDATES = {
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
    "Linux": ["google-chrome", "chromium", "chromium-browser", "microsoft-edge"],
    "Windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ],
}

PDF_LINK_JS = """
JSON.stringify(Array.from(document.querySelectorAll('a'))
  .map(a => a.href)
  .filter(h => /pdf/i.test(h) && !/supplement|appendix|supporting/i.test(h))
  .slice(0, 12))
"""


def find_chrome() -> Optional[str]:
    for cand in CHROME_CANDIDATES.get(platform.system(), []):
        if os.path.isabs(cand) and Path(cand).exists():
            return cand
        found = shutil.which(cand)
        if found:
            return found
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def _page_ws(port: int, timeout: float = 15.0) -> Optional[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as fh:
                for t in json.loads(fh.read().decode()):
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.3)
    return None


def grab(url_or_doi: str, workdir: Path, timeout: float = 50.0) -> Tuple[Optional[Path], str]:
    """Return (pdf_path, note). Never raises."""
    try:
        import websocket  # type: ignore
    except Exception:
        return None, "websocket-client not installed (pip install litfetch[browser])"

    chrome = find_chrome()
    if not chrome:
        return None, "no Chrome/Chromium/Edge found on this machine"

    url = url_or_doi
    if not url.lower().startswith("http"):
        url = "https://doi.org/" + url

    port = _free_port()
    profile = tempfile.mkdtemp(prefix="litfetch-profile-")
    proc = None
    ws = None
    try:
        proc = subprocess.Popen(
            [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--remote-allow-origins=*",
             f"--remote-debugging-port={port}",
             f"--user-data-dir={profile}", "about:blank"],
            env=_clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        page_ws = _page_ws(port)
        if not page_ws:
            return None, "Chrome exposed no page target (sandbox?)"

        ws = websocket.create_connection(page_ws, timeout=timeout, suppress_origin=True)
        ids = {"n": 0}
        out = workdir / "browser-tier.pdf"
        state = {"tried_link": False, "eval_id": None, "pdf_requested": False}

        def send(method: str, params: Optional[dict] = None) -> int:
            ids["n"] += 1
            ws.send(json.dumps({"id": ids["n"], "method": method, "params": params or {}}))
            return ids["n"]

        send("Page.enable")
        send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})
        send("Page.navigate", {"url": url})

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                continue
            msg = json.loads(raw)
            method = msg.get("method", "")
            params = msg.get("params", {})

            if method == "Page.loadEventFired" and not state["tried_link"]:
                state["tried_link"] = True
                state["eval_id"] = send("Runtime.evaluate", {
                    "expression": PDF_LINK_JS, "returnByValue": True,
                })

            elif state["eval_id"] is not None and msg.get("id") == state["eval_id"]:
                state["eval_id"] = None
                value = (msg.get("result", {}).get("result", {}) or {}).get("value")
                links = []
                if value:
                    try:
                        links = json.loads(value)
                    except Exception:
                        links = []
                if links:
                    state["pdf_requested"] = True
                    send("Page.navigate", {"url": links[0]})

            elif method == "Fetch.requestPaused":
                rid = params.get("requestId")
                headers = {h["name"].lower(): h["value"]
                           for h in params.get("responseHeaders", [])}
                if "pdf" in headers.get("content-type", "").lower():
                    body_id = send("Fetch.getResponseBody", {"requestId": rid})
                    while True:
                        rep = json.loads(ws.recv())
                        if rep.get("id") != body_id:
                            if rep.get("method") == "Fetch.requestPaused":
                                send("Fetch.continueRequest",
                                     {"requestId": rep["params"]["requestId"]})
                            continue
                        body = rep.get("result", {})
                        if body.get("base64Encoded"):
                            data = base64.b64decode(body.get("body", ""))
                        else:
                            data = body.get("body", "").encode("utf-8", "replace")
                        try:
                            send("Fetch.failRequest", {"requestId": rid,
                                                       "errorReason": "Aborted"})
                        except Exception:
                            pass
                        out.write_bytes(data)
                        return out, f"captured {len(data)} bytes via CDP"
                else:
                    send("Fetch.continueRequest", {"requestId": rid})

        if state["pdf_requested"]:
            return None, "followed a PDF link but no PDF response arrived"
        return None, "timed out before a PDF response was seen"
    except Exception as exc:  # pragma: no cover - environment dependent
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
