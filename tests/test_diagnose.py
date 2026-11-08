import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litfetch.io import diagnose  # noqa: E402


def test_classify_accepts_pdf_magic():
    assert diagnose.classify(200, "application/pdf", b"%PDF-1.7 ...", 9999)[0] == "pdf"


def test_classify_cloudflare_interstitial():
    body = b"<html><head><title>Just a moment...</title></head><body>checking your browser</body>"
    assert diagnose.classify(403, "text/html", body, len(body))[0] == "cloudflare"


def test_classify_captcha_challenge():
    body = b"<html><body>Please verify you are human before continuing</body></html>"
    assert diagnose.classify(200, "text/html", body, len(body))[0] == "challenge"


def test_classify_paywall_phrasing():
    body = b"<html><body>Purchase this article to continue reading. Sign in to view.</body></html>"
    assert diagnose.classify(200, "text/html", body, len(body))[0] == "paywall"


def test_classify_dead_and_server_error():
    assert diagnose.classify(404, "text/html", b"not found", 9)[0] == "dead"
    assert diagnose.classify(502, "text/html", b"<html>bad gateway</html>", 20)[0] == "js_required"


def test_every_verdict_has_an_action():
    for code in ("pdf", "cloudflare", "challenge", "paywall", "js_required",
                 "dead", "not_pdf", "error"):
        assert code in diagnose.ACTION and diagnose.ACTION[code]


def test_summarise_prefers_bot_wall():
    probes = [diagnose.Probe(url="u", status=403, verdict="cloudflare"),
              diagnose.Probe(url="v", status=404, verdict="dead")]
    assert "bot wall" in diagnose.summarise(probes)
    assert diagnose.summarise([diagnose.Probe(url="u", verdict="pdf")]) == "retrievable"


def test_classify_incapsula_wall():
    body = (b'<html><head><script src="/Milke-a-dishd"></script>'
            b'<script>sessionStorage.setItem("distil_referrer","")</script></head>'
            b'<body><iframe src="/_Incapsula_Resource?SWUDNSAI=31"></iframe></body></html>')
    assert diagnose.classify(200, "text/html", body, len(body))[0] == "botwall"


def test_classify_incapsula_wall():
    body = (b'<html><head><script src="/Milke-a-dishd"></script>'
            b'<script>sessionStorage.setItem("distil_referrer","")</script></head>'
            b'<body><iframe src="/_Incapsula_Resource?SWUDNSAI=31"></iframe></body></html>')
    assert diagnose.classify(200, "text/html", body, len(body))[0] == "botwall"
