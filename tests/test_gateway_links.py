"""Gateway extract_links: same-site check ignores "www." and the port (VM 507, 2026-11-14)."""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "api"))
_spec = importlib.util.spec_from_file_location("gateway_main_links", _ROOT / "api" / "app" / "main.py")
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)

PAGE = """<html><body><main>
<p>Úvod školy s dostatkem textu.</p>
<a href="https://skola.cz/kontakt-a-informace-pro-rodice/">bare host</a>
<a href="https://www.skola.cz/jak-se-ucit-doma-strategie/">www host</a>
<a href="https://SKOLA.cz:443/dokument.pdf">pdf, port and case</a>
<a href="https://jina-skola.cz/clanek-o-necem/">other site</a>
</main></body></html>"""


def urls(source):
    return [l["url"] for l in gw.extract_links(PAGE, source, 10)]


def test_www_source_keeps_bare_host_links():
    got = urls("https://www.skola.cz/")
    assert any("kontakt-a-informace" in u for u in got)
    assert any("jak-se-ucit-doma" in u for u in got)
    assert any(u.endswith("dokument.pdf") for u in got)
    assert not any("jina-skola" in u for u in got)


def test_bare_source_keeps_www_links():
    got = urls("https://skola.cz/")
    assert any("jak-se-ucit-doma" in u for u in got)
    assert not any("jina-skola" in u for u in got)


def test_site_host():
    assert gw._site_host("https://WWW.Skola.cz:8443/x") == "skola.cz"
    assert gw._site_host("https://skola.cz") == "skola.cz"
    assert gw._site_host("https://wwwskola.cz") == "wwwskola.cz"
