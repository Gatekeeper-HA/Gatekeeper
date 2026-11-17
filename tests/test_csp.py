"""The web page's Content Security Policy (csp.py), on every page as it's served."""

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import pytest

from gatekeeper import config, csp, web
from gatekeeper.live import LivePage
from gatekeeper.notify import links_911
from gatekeeper.settings_page import SettingsPage
from gatekeeper.visits import Labels, VisitsPage
from gatekeeper.zones import Zones, ZonesPage, ZoneStore

PASSWORD = "porch-light"
AT = 1_800_000_000.0


class FrigateZone:
    polygon = [(0.48, 0.78), (1.0, 0.78), (1.0, 1.0), (0.48, 1.0)]


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Every page, with a visit whose words would be a script if they weren't escaped."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    log = tmp_path / "events.jsonl"
    visit = {
        "ts": AT,
        "event_id": f"{AT:.6f}-abc123",
        "trigger": "person",
        "outcome": "completed",
        "classification": "likely_delivery",
        "transcript": "<script>alert(1)</script><img src=x onerror=alert(2)>",
    }
    log.write_text(json.dumps(visit) + "\n", encoding="utf-8")
    visits = VisitsPage(
        log,
        Labels(tmp_path / "labels.json"),
        snapshot=lambda event_id: None,
        snapshot_dir=tmp_path,
        clock=lambda: AT + 60,
    )
    zones = ZonesPage(
        Zones(ZoneStore(tmp_path / "zones.json", "front_door"), lambda name: FrigateZone()),
        {"porch": "visitors here are greeted", "door": "the front door"},
        frame=lambda: b"JPEG",
    )
    live = LivePage("http://go2rtc", "a", "b", emergency=links_911("1 Main St", "Help"))
    server = web.serve(
        web.FacesPage(tmp_path / "faces", enabled=True, candidate_days=14),
        PASSWORD,
        port=0,
        visits=visits,
        zones=zones,
        live=live,
        settings_page=SettingsPage(config.load(), restart=lambda: None),
    )
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


def get(base, path, password=PASSWORD):
    auth = base64.b64encode(f"a:{password}".encode()).decode()
    request = urllib.request.Request(base + path, headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(request) as r:
            return r.status, r.read().decode(errors="replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), e.headers


def directives(policy: str) -> dict[str, list[str]]:
    return {d.split()[0]: d.split()[1:] for d in policy.split("; ")}


@pytest.mark.parametrize("path", ["", "faces/", "zones/", "live/", "settings/", "login"])
def test_every_page_keeps_to_the_policy_it_is_sent(site, path):
    status, page, headers = get(site, path)
    assert status == 200 and "<h1>" in page
    policy = directives(headers["Content-Security-Policy"])
    assert policy["default-src"] == ["'none'"]
    assert policy["script-src"] == ["'self'"]  # never inline
    assert policy["frame-ancestors"] == ["'self'"] and policy["base-uri"] == ["'none'"]

    # Scripts: only from files, and those files are served.
    tags = re.findall(r"<script\b([^>]*)>(.*?)</script>", page, re.S)
    assert tags, "every page registers the app's service worker"
    for attributes, body in tags:
        assert body == "" and (m := re.search(r'src="([^"]+)"', attributes))
        url = urllib.parse.urljoin(site + path, m[1])
        status, _, js = get(url, "", password="")  # served without logging in
        assert status == 200 and js["Content-Type"].startswith("text/javascript")
    # Nothing that would run or style from an attribute.
    assert not re.search(r"<[^>]*\son\w+\s*=", page)
    assert not re.search(r"<[^>]*\sstyle\s*=", page)
    assert "javascript:" not in page
    # Each <style>, by its hash.
    styles = re.findall(r"<style>(.*?)</style>", page, re.S)
    assert styles
    for css in styles:
        digest = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in policy["style-src"]


def test_what_a_visitor_said_stays_text(site):
    page = get(site, "")[1]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x" not in page


def test_only_pages_carry_it(site):
    for path in ("app.js", "manifest.webmanifest", "icon-192.png", "zones/frame.jpg"):
        status, _, headers = get(site, path)
        assert status == 200 and "Content-Security-Policy" not in headers


def test_the_policy_names_each_style_once():
    css = "body { color: red; }"
    assert csp.style(css) == f"<style>{css}</style>"
    csp.style(css)
    digest = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
    assert csp.policy().count(f"'sha256-{digest}'") == 1
