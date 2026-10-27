import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import pytest

from gatekeeper import web
from gatekeeper.visits import LABELS, Labels, VisitsPage, describe, labelable, read_visits

PASSWORD = "porch-light"
NOW = 1_792_975_800.0  # Sun 25 Oct 2026, 19:50 in the rig's time


def record(event_id, ts, outcome="completed", **fields):
    return {"ts": ts, "event_id": event_id, "trigger": "person", "outcome": outcome, **fields}


@pytest.fixture
def log(tmp_path):
    """A visit log with a day's file before it: the Instacart evening, and older."""
    path = tmp_path / "events.jsonl"
    older = [record("1792100000.000000-old", 1792100030.0, classification="likely_delivery")]
    (tmp_path / "events-2026-10-23.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in older), encoding="utf-8"
    )
    rows = [
        record("1792975409.168628-p16wn1", 1792975434.6, classification="no_response"),
        record(
            "1792975409.168628-p16wn1",
            1792975440.0,
            outcome="quick_reply",
            trigger="quick_reply",
            details={"reply": "coming", "result": "said"},
        ),  # fmt: skip
        {
            "ts": 1792975505.4,
            "event_id": "package-1792975469.647",
            "trigger": "package",
            "outcome": "package_delivered",
        },  # fmt: skip
        record("1792975523.535438-oqfxkg", 1792975548.6, outcome="delivery_trip"),
        record(
            "press-1792975600.000",
            1792975620.0,
            trigger="button",
            classification="likely_delivery",
            transcript="Your pizza <is> here",
        ),  # fmt: skip
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8")
    return path


def page_for(log, tmp_path, snapshots=None):
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(exist_ok=True)
    (snapshot_dir / "package-1792975469.647-delivered.jpg").write_bytes(b"PACKAGE")
    snapshots = snapshots if snapshots is not None else {}
    return VisitsPage(
        log,
        Labels(tmp_path / "visit-labels.json", clock=lambda: NOW),
        snapshot=snapshots.get,
        snapshot_dir=snapshot_dir,
        clock=lambda: NOW,
    )


def test_visits_are_read_from_the_log_and_its_daily_files(log):
    assert len(read_visits(log, 0)) == 6  # the bad line skipped
    assert len(read_visits(log, 1792975000)) == 5


def test_visits_are_newest_first_with_their_labels_and_no_replies(log, tmp_path):
    page = page_for(log, tmp_path)
    page.labels.set("1792975523.535438-oqfxkg", "groceries")
    rows = page.visits(days=3)
    assert [r["event_id"] for r in rows] == [
        "press-1792975600.000",
        "1792975523.535438-oqfxkg",
        "package-1792975469.647",
        "1792975409.168628-p16wn1",
    ]
    assert rows[1]["label"] == "groceries" and rows[0]["label"] == ""
    assert len(page.visits(days=30)) == 5  # and the older day


def test_what_gatekeeper_did_in_words():
    assert describe(record("a", 1, classification="no_response")) == "Greeted, no answer"
    assert describe(record("a", 1, outcome="left")).startswith("Taken for the household")
    assert describe(record("a", 1, outcome="delivery_trip")) == (
        "Another trip of a delivery (not greeted)"
    )
    assert describe(
        record("a", 1, outcome="household", details={"name": "Sam", "greeted": False})
    ) == ("Recognized as Sam (not greeted)")
    assert describe(record("a", 1, trigger="button", classification="likely_delivery")) == (
        "Doorbell pressed: greeted, said it's a delivery"
    )
    assert describe(record("a", 1, flow="law_enforcement")) == "Talked with an officer"
    assert not labelable({"trigger": "package", "outcome": "package_delivered"})
    assert labelable(record("a", 1, trigger="button"))


def test_labels_are_kept_and_cleared(tmp_path):
    labels = Labels(tmp_path / "logs" / "visit-labels.json", clock=lambda: NOW)
    labels.set("1792897282.415415-nlet2z", "food")
    assert labels.all() == {"1792897282.415415-nlet2z": {"label": "food", "at": NOW}}
    labels.set("1792897282.415415-nlet2z", "")
    assert labels.all() == {}
    with pytest.raises(ValueError):
        labels.set("1792897282.415415-nlet2z", "pizza-guy")
    with pytest.raises(ValueError):
        labels.set("../../etc/passwd", "food")
    assert {code for code, _ in LABELS} >= {"amazon", "ups", "fedex", "food", "groceries"}


def test_pictures_come_from_the_package_photo_or_the_camera(log, tmp_path):
    page = page_for(log, tmp_path, snapshots={"1792975409.168628-p16wn1": b"PERSON"})
    assert page.image("package-1792975469.647") == b"PACKAGE"
    assert page.image("1792975409.168628-p16wn1") == b"PERSON"
    assert page.image("1792975523.535438-oqfxkg") is None  # expired at the camera
    assert page.image("press-1792975600.000") is None
    assert page.image("../escape") is None


def test_the_page_shows_each_visit_and_a_label_for_people(log, tmp_path):
    page = page_for(log, tmp_path)
    page.token = "T"
    page.labels.set("1792975523.535438-oqfxkg", "groceries")
    text = page.render(days=3)
    assert "<h2>Sunday 25 October</h2>" in text
    assert "Another trip of a delivery" in text and "Package left at the door" in text
    assert "Heard: &ldquo;Your pizza &lt;is&gt; here&rdquo;" in text  # escaped
    assert text.count('action="label"') == 3  # not for the package
    assert '<option value="groceries" selected>' in text
    assert 'src="visit/1792975409.168628-p16wn1.jpg"' in text
    assert "visit/press-" not in text  # no picture for a press with nobody seen
    assert "1 of 4 labelled" in text
    assert '<a href="faces/">Faces</a>' in text


class Client:
    def __init__(self, faces, visits):
        self.server = web.serve(faces, PASSWORD, port=0, visits=visits)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.auth = "Basic " + base64.b64encode(f"anyone:{PASSWORD}".encode()).decode()

    def get(self, path=""):
        req = urllib.request.Request(self.base + path, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(self, action, fields):
        req = urllib.request.Request(
            self.base + action,
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


@pytest.fixture
def client(log, tmp_path):
    faces = web.FacesPage(tmp_path / "faces", enabled=True, candidate_days=14)
    visits = page_for(log, tmp_path, snapshots={"1792975409.168628-p16wn1": b"PERSON"})
    c = Client(faces, visits)
    c.visits = visits
    yield c
    c.server.shutdown()


def test_visits_at_the_top_and_faces_under_it(client):
    status, body = client.get()
    assert status == 200 and b"<h1>Visits</h1>" in body
    status, body = client.get("faces/")
    assert status == 200 and b"<h1>Household faces</h1>" in body
    assert b'<a href="../">Visits</a>' in body
    assert client.get("visit/1792975409.168628-p16wn1.jpg") == (200, b"PERSON")
    assert client.get("visit/1792975523.535438-oqfxkg.jpg")[0] == 404
    assert b"1 day(s)" in client.get("?days=1")[1]


def test_a_visit_is_labelled_from_the_page(client):
    token = re.search(r'name=token value="([^"]+)"', client.get()[1].decode())[1]
    status, _, headers = client.post(
        "label", {"token": token, "visit": "1792975409.168628-p16wn1", "label": "groceries"}
    )
    assert status == 303 and headers["Location"].startswith("./?done=Saved")
    assert client.visits.labels.all()["1792975409.168628-p16wn1"]["label"] == "groceries"
    # The faces page's token works for both: one page, one token.
    assert client.visits.token == token
    status, body, _ = client.post(
        "label", {"token": "stale", "visit": "1792975409.168628-p16wn1", "label": "food"}
    )
    assert status == 403 and "<h1>Visits</h1>" in body
    status, body, _ = client.post(
        "label", {"token": token, "visit": "1792975409.168628-p16wn1", "label": "pizza"}
    )
    assert status == 400 and "Pick what the visit was" in body


def test_without_the_visits_page_the_faces_stay_at_the_top(tmp_path):
    faces = web.FacesPage(tmp_path / "faces", enabled=True, candidate_days=14)
    server = web.serve(faces, PASSWORD, port=0)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/",
            headers={"Authorization": "Basic " + base64.b64encode(b"a:porch-light").decode()},
        )
        with urllib.request.urlopen(req) as r:
            assert b"<h1>Household faces</h1>" in r.read()
    finally:
        server.shutdown()
