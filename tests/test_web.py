import base64
import re
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pytest

from gatekeeper import web
from gatekeeper.faces import Gallery

PASSWORD = "porch-light"


def unit(*values):
    v = np.array(values + (0.0,) * (128 - len(values)))
    return v / np.linalg.norm(v)


def kept(face_dir, event_id, embeddings, at=1_792_000_000.0):
    """A candidate file, as FaceWatcher.keep writes it."""
    d = face_dir / "candidates"
    d.mkdir(parents=True, exist_ok=True)
    n = len(embeddings)
    with (d / f"{event_id}.npz").open("wb") as f:
        np.savez_compressed(
            f,
            embeddings=np.array(embeddings, dtype=np.float32),
            faces=np.full((n, 112, 112, 3), 128, np.uint8),
            at=np.full(n, at),
            outcome=np.array("entered"),
            names=np.array([""] * n),
        )


class Client:
    """The page over real HTTP, as a browser with the password would use it."""

    def __init__(self, page, password=PASSWORD):
        self.page = page
        self.server = web.serve(page, PASSWORD, port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.auth = "Basic " + base64.b64encode(f"anyone:{password}".encode()).decode()

    def get(self, path=""):
        req = urllib.request.Request(self.base + path, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def post(self, action, fields):
        body = urllib.parse.urlencode(fields, doseq=True).encode()
        req = urllib.request.Request(
            self.base + action, body, headers={"Authorization": self.auth}, method="POST"
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
def faces(tmp_path):
    face_dir = tmp_path / "faces"
    kept(face_dir, "e1", [unit(1, 0.1)])
    kept(face_dir, "e2", [unit(1, 0.15)])
    kept(face_dir, "e3", [unit(0, 0, 1)])  # someone else
    page = web.FacesPage(face_dir, enabled=True, candidate_days=14, encode=lambda bgr: b"JPEG")
    client = Client(page)
    yield client
    client.server.shutdown()


def test_the_page_needs_the_password(faces):
    wrong = Client.__new__(Client)
    wrong.base, wrong.auth = faces.base, "Basic " + base64.b64encode(b"x:guess").decode()
    status, body, _ = wrong.get()  # sent to the login page
    assert status == 200 and b"<input type=password name=password" in body
    assert wrong.get("face/e1.npz/0.jpg")[0] == 401


def test_the_kept_faces_are_shown_grouped_by_person(faces):
    status, body, headers = faces.get()
    page = body.decode()
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert "Faces to enroll (3)" in page
    groups = re.findall(r"<strong>Group (\d)</strong>: (\d) face", page)
    assert groups == [("1", "2"), ("2", "1")]
    assert 'src="face/e1.npz/0.jpg"' in page and "<script" not in page
    status, jpeg, headers = faces.get("face/e1.npz/0.jpg")
    assert (status, jpeg, headers["Content-Type"]) == (200, b"JPEG", "image/jpeg")


def test_only_kept_faces_can_be_fetched(faces):
    for path in ("face/..%2F..%2Fgallery.json/0.jpg", "face/e1.npz/9.jpg", "face/nope.npz/0.jpg"):
        assert faces.get(path)[0] == 404


def test_enrolling_ticked_faces_needs_a_name_and_consent(faces):
    token = faces.page.token
    chosen = ["e1.npz:0", "e2.npz:0"]
    status, body, _ = faces.post("enroll", {"token": token, "face": chosen, "name": "Alex"})
    assert status == 400 and "agreed to be recognized" in body
    status, body, _ = faces.post(
        "enroll", {"token": token, "face": chosen, "name": "<b>", "consent": "yes"}
    )
    assert status == 400 and "A name is" in body

    status, _, headers = faces.post(
        "enroll", {"token": token, "face": chosen, "name": " Alex  Smith ", "consent": "yes"}
    )
    assert status == 303 and headers["Location"].startswith("./?done=Enrolled+2")
    gallery = Gallery(faces.page.face_dir / "gallery.json")
    assert gallery.match(unit(1, 0.12))[0] == "Alex Smith"
    page = faces.get()[1].decode()
    assert "Faces to enroll (1)" in page and "Alex Smith" in page
    assert '<option value="Alex Smith">' in page  # offered for the next group


def test_changes_need_the_pages_token(faces):
    status, body, _ = faces.post(
        "enroll", {"token": "forged", "face": ["e1.npz:0"], "name": "Alex", "consent": "yes"}
    )
    assert status == 403 and "out of date" in body
    assert not (faces.page.face_dir / "gallery.json").exists()


def test_faces_that_arent_the_household_can_be_deleted(faces):
    status, _, _ = faces.post("discard", {"token": faces.page.token, "face": ["e3.npz:0"]})
    assert status == 303
    assert "Faces to enroll (2)" in faces.get()[1].decode()


def test_forget_and_purge(faces, tmp_path):
    Gallery(faces.page.face_dir / "gallery.json").enroll("Sam <3", [unit(0, 1)])
    page = faces.get()[1].decode()
    assert "Sam &lt;3" in page and "Sam <3" not in page  # escaped
    assert faces.post("forget", {"token": faces.page.token, "name": "Sam <3"})[0] == 303
    assert Gallery(faces.page.face_dir / "gallery.json").names == []

    status, body, _ = faces.post("purge", {"token": faces.page.token, "confirm": "yes"})
    assert status == 400 and '"delete"' in body
    assert faces.post("purge", {"token": faces.page.token, "confirm": "Delete"})[0] == 303
    assert not (faces.page.face_dir / "candidates").exists()
    assert "Faces to enroll (0)" in faces.get()[1].decode()


def test_behind_a_path_prefix_links_stay_relative(faces):
    # Home Assistant's ingress serves the page under /api/hassio_ingress/<token>/.
    status, body, _ = faces.get("api/hassio_ingress/abc/")
    assert status == 200 and 'action="enroll"' in body.decode()
    assert faces.get("api/hassio_ingress/abc/face/e1.npz/0.jpg")[1] == b"JPEG"


def test_the_page_says_when_face_recognition_is_off(tmp_path):
    page = web.FacesPage(tmp_path, enabled=False, candidate_days=14)
    assert "Face recognition is off" in page.render()
    assert "None yet" in page.render()


def test_a_group_like_someone_enrolled_is_filled_in_with_their_name(faces):
    Gallery(faces.page.face_dir / "gallery.json").enroll("Alex", [unit(1, 0.1)])
    Gallery(faces.page.face_dir / "gallery.json").enroll("Sam", [unit(0, 1)])
    page = faces.get()[1].decode()
    # e1 + e2 look like Alex: filled in. e3 (someone else) looks like no one: left blank.
    assert "Looks like <strong>Alex</strong>" in page and 'value="Alex"' in page
    assert page.count('value=""') == 1 and "not sure" in page


def test_capture_at_the_door(tmp_path):
    asked = []
    page = web.FacesPage(
        tmp_path,
        enabled=True,
        candidate_days=14,
        capture=lambda: asked.append(1),
        capturing=lambda: 90.0,
    )
    client = Client(page)
    try:
        status, _, headers = client.post("capture", {"token": page.token})
        assert (
            status == 303
            and asked == [1]
            and "Capturing" in urllib.parse.unquote_plus(headers["Location"])
        )
        assert "Capturing faces at the door: 90 s left" in client.get()[1].decode()
    finally:
        client.server.shutdown()
    off = web.FacesPage(tmp_path, enabled=False, candidate_days=14)
    assert "Capture at the door" not in off.render()
    with pytest.raises(ValueError, match="off"):
        off.capture()


def test_the_page_shows_the_sharper_picture_when_there_is_one(tmp_path):
    face_dir = tmp_path / "faces"
    (face_dir / "candidates").mkdir(parents=True)
    with (face_dir / "candidates" / "e9.npz").open("wb") as f:
        np.savez_compressed(
            f,
            embeddings=np.array([unit(1)], np.float32),
            faces=np.zeros((1, 112, 112, 3), np.uint8),
            at=np.array([1.0]),
            outcome=np.array("capture"),
            names=np.array([""]),
            display=np.frombuffer(b"SHARP", np.uint8),
            display_ends=np.array([5]),
        )
    page = web.FacesPage(face_dir, enabled=True, candidate_days=14, encode=lambda bgr: b"SMALL")
    assert page.face_jpeg("e9.npz", 0) == b"SHARP"
