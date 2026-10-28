import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import pytest

from gatekeeper import web
from gatekeeper.zones import Zone, Zones, ZonesPage, ZoneStore, parse_points

PASSWORD = "door-secret"
FRIGATES = [(0.48, 0.78), (1.0, 0.78), (1.0, 1.0), (0.48, 1.0)]
OURS = [(0.5, 0.7), (1.0, 0.7), (1.0, 1.0), (0.5, 1.0)]


class FrigateZone:
    def __init__(self, polygon):
        self.polygon = polygon


def test_the_store_keeps_each_cameras_zones(tmp_path):
    path = tmp_path / "logs" / "zones.json"
    ZoneStore(path, "back_yard").put("lawn", OURS)
    store = ZoneStore(path, "front_door")
    store.put("porch", OURS)
    store.put("door", [(0.7, 0.85), (1.0, 0.85), (1.0, 1.0)])
    store.put("door", None)
    assert ZoneStore(path, "front_door").get("porch") == OURS
    assert ZoneStore(path, "front_door").get("door") is None
    assert ZoneStore(path, "back_yard").get("lawn") == OURS
    assert set(json.loads(path.read_text())["cameras"]) == {"back_yard", "front_door"}


def test_a_broken_file_is_no_zones(tmp_path, caplog):
    path = tmp_path / "zones.json"
    path.write_text("{not json")
    assert ZoneStore(path, "front_door").get("porch") is None
    assert "using Frigate's zones" in caplog.text


def test_a_zone_is_ours_once_drawn_else_frigates(tmp_path):
    store = ZoneStore(tmp_path / "zones.json", "front_door")
    frigate = FrigateZone(None)
    zone = Zone("porch", store, frigate)
    assert (zone.source, zone.polygon, zone.contains((0.9, 0.9))) == (None, None, None)
    frigate.polygon = FRIGATES  # Frigate's config has been read
    assert (zone.source, zone.polygon) == ("frigate", FRIGATES)
    assert zone.contains((0.49, 0.75)) is False
    store.put("porch", OURS)  # drawn on the page: at once
    assert (zone.source, zone.polygon) == ("gatekeeper", OURS)
    assert zone.contains((0.51, 0.75)) is True
    assert Zone("porch", ZoneStore(tmp_path / "none.json", "x")).polygon is None


def test_each_zone_is_asked_of_frigate_once(tmp_path):
    asked = []
    zones = Zones(
        ZoneStore(tmp_path / "zones.json", "front_door"),
        lambda name: asked.append(name) or FrigateZone(FRIGATES),
    )
    assert zones.zone("porch") is zones.zone("porch")
    zones.zone("door")
    assert asked == ["porch", "door"]


def test_points_are_fractions_of_the_frame():
    assert parse_points(" 0.1,0.2  0.3,0.2;0.3,0.4 ") == [(0.1, 0.2), (0.3, 0.2), (0.3, 0.4)]
    assert parse_points("  ") is None
    for text, why in [
        ("0.1,0.2 0.3,0.2", "at least 3"),
        ("0.1,0.2 0.3,0.2 1.5,0.4", "outside the frame"),
        ("0.1,0.2 0.3 0.3,0.4", "isn't a point"),
        (" ".join(["0.1,0.1"] * 25), "at most 24"),
    ]:
        with pytest.raises(ValueError, match=why):
            parse_points(text)


@pytest.fixture
def zones_page(tmp_path):
    zones = Zones(ZoneStore(tmp_path / "zones.json", "front_door"), lambda n: FrigateZone(FRIGATES))
    return ZonesPage(
        zones,
        {"porch": "visitors here are greeted", "door": "the front door"},
        frame=lambda: b"JPEG",
    )


def test_saving_draws_a_zone_or_gives_it_back_to_frigate(zones_page):
    store = zones_page.zones.store
    done = zones_page.save(
        {"zone:porch": "0.5,0.7 1,0.7 1,1 0.5,1", "zone:door": "", "zone:x": "1"}
    )
    assert done == "Saved 1 zone(s)." and store.get("porch") == OURS
    assert zones_page.save({"zone:porch": "0.500,0.700 1,0.7 1,1 0.5,1"}) == "Nothing changed."
    assert zones_page.save({"zone:porch": ""}) == "Saved 1 zone(s)."
    assert store.get("porch") is None
    # One bad zone: nothing is kept.
    with pytest.raises(ValueError, match="The door zone: a zone needs at least 3 points"):
        zones_page.save({"zone:porch": "0.5,0.7 1,0.7 1,1", "zone:door": "0.7,0.85"})
    assert store.get("porch") is None


def test_the_page_draws_each_zone_on_the_picture(zones_page):
    zones_page.zones.store.put("porch", OURS)
    zones_page.token = "tok"
    body = zones_page.render()
    assert '<polygon id="shape-porch" points="0.5,0.7 1.0,0.7 1.0,1.0 0.5,1.0"' in body
    assert '<polygon id="shape-door" points="0.48,0.78 1.0,0.78 1.0,1.0 0.48,1.0"' in body
    assert 'name="zone:porch" data-zone="porch" value="0.500,0.700 1.000,0.700' in body
    assert 'name="zone:door" data-zone="door" value="" placeholder="0.480,0.780' in body
    assert "Now drawn here." in body and "Now Frigate's (blank: keep using it)." in body
    assert 'name=token value="tok"' in body and '<img src="frame.jpg"' in body
    assert '<a href="../">Visits</a> <a href="../faces/">Faces</a> <strong>Zones</strong>' in body


class Client:
    def __init__(self, server):
        self.server = server
        self.base = f"http://127.0.0.1:{server.server_address[1]}/"
        self.auth = "Basic " + base64.b64encode(f"anyone:{PASSWORD}".encode()).decode()

    def get(self, path=""):
        req = urllib.request.Request(self.base + path, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(self, path, fields):
        req = urllib.request.Request(
            self.base + path,
            urllib.parse.urlencode(fields).encode(),
            headers={"Authorization": self.auth},
            method="POST",
        )
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def test_the_zones_page_on_the_web(tmp_path, zones_page):
    faces = web.FacesPage(tmp_path / "faces", enabled=True, candidate_days=14)
    client = Client(web.serve(faces, PASSWORD, port=0, zones=zones_page))
    try:
        status, body = client.get("zones/")
        assert status == 200 and b"<h1>Zones</h1>" in body
        assert client.get("zones/frame.jpg") == (200, b"JPEG")
        token = re.search(r'name=token value="([^"]+)"', body.decode())[1]
        status, _, headers = client.post(
            "zones/save", {"token": token, "zone:porch": "0.5,0.7 1,0.7 1,1 0.5,1"}
        )
        assert status == 303 and headers["Location"] == "./?done=Saved+1+zone%28s%29."
        assert zones_page.zones.store.get("porch") == OURS
        assert b"Saved 1 zone(s)." in client.get("zones/?done=Saved+1+zone%28s%29.")[1]
        status, body, _ = client.post("zones/save", {"token": "stale", "zone:porch": ""})
        assert status == 403 and "<h1>Zones</h1>" in body
        status, body, _ = client.post("zones/save", {"token": token, "zone:porch": "1,1"})
        assert status == 400 and "at least 3 points" in body
        assert zones_page.zones.store.get("porch") == OURS
    finally:
        client.server.shutdown()


def test_the_zones_gatekeeper_uses():
    from gatekeeper.app import used_zones
    from gatekeeper.config import Settings

    settings = Settings(trigger_zones="porch,walkway", door_zone="door", package_zone="porch")
    assert list(used_zones(settings)) == ["porch", "walkway", "door"]
    assert used_zones(Settings(trigger_zones="", door_zone="", package_zone="")) == {}
