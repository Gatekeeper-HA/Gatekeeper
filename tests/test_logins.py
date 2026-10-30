import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from gatekeeper import logins, web
from gatekeeper.logins import Logins, safe_next

PASSWORD = "porch-light"


class Browser:
    """A browser: no password sent with each request, a cookie jar, redirects as asked."""

    def __init__(self, base):
        self.base = base
        self.cookies = {}

    def request(self, path, data=None, follow=False):
        headers = {}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        opener = urllib.request.build_opener(*([] if follow else [NoRedirect]))
        try:
            with opener.open(req) as r:
                status, body, headers = r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            status, body, headers = e.code, e.read().decode(), e.headers
        for cookie in headers.get_all("Set-Cookie") or []:
            name, _, rest = cookie.partition("=")
            value = rest.split(";", 1)[0]
            if "Max-Age=0" in cookie:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return status, body, headers

    def log_in(self, password, target=""):
        form = urllib.parse.urlencode({"password": password, "next": target}).encode()
        return self.request("login", form)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@pytest.fixture
def site(tmp_path):
    faces = web.FacesPage(tmp_path / "faces", enabled=True, candidate_days=14)
    store = Logins(tmp_path / "web-logins.json")
    server = web.serve(faces, PASSWORD, port=0, logged_in=store)
    yield f"http://127.0.0.1:{server.server_address[1]}/", store, tmp_path
    server.shutdown()


def test_a_page_sends_you_to_log_in_and_back(site):
    base, store, tmp_path = site
    b = Browser(base)
    status, _, headers = b.request("faces/?x=1")
    assert status == 303 and headers["Location"] == "../login?next=faces%2F%3Fx%3D1"
    status, body, _ = b.request("login?next=faces/")
    assert status == 200 and 'name=next value="faces/"' in body
    assert "autocomplete=current-password" in body

    status, body, _ = b.log_in("guess", "faces/")
    assert status == 401 and "not the password" in body and not b.cookies

    status, _, headers = b.log_in(PASSWORD, "faces/")
    assert status == 303 and headers["Location"] == "faces/"
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" not in cookie
    status, body, _ = b.request("faces/")
    assert status == 200 and "Household faces" in body and "Log out" in body
    # Kept hashed: a restart keeps the login, but the file doesn't hold the cookie.
    token = b.cookies[logins.COOKIE]
    assert token not in (tmp_path / "web-logins.json").read_text()
    assert Logins(tmp_path / "web-logins.json").valid(token)

    status, _, headers = b.request("faces/logout")  # from a page one level down
    assert status == 303 and headers["Location"] == "../login" and not b.cookies
    assert not store.valid(token)
    assert b.request("faces/")[0] == 303  # logged out


def test_anything_but_a_page_is_refused_without_a_login(site):
    base, _, _ = site
    b = Browser(base)
    assert b.request("face/e1.npz/0.jpg")[0] == 401
    assert b.request("enroll", b"name=x")[0] == 401


def test_where_to_go_after_logging_in_stays_on_the_site():
    for good in ["", "faces/", "zones/", "live/", "./", "?days=7", "faces/?x=1"]:
        assert safe_next(good) == (good or "./")
    for bad in ["//evil.example/", "https://evil.example/", "../../etc", "/abs", "a b"]:
        assert safe_next(bad) == "./"


def test_logins_end(tmp_path):
    now = [1_800_000_000.0]
    store = Logins(tmp_path / "l.json", clock=lambda: now[0])
    token = store.start()
    now[0] += 29 * 86400
    assert store.valid(token)  # and that use moves its end on
    now[0] += 29 * 86400
    assert store.valid(token)
    now[0] += 31 * 86400
    assert not store.valid(token) and not store.valid("") and not store.valid("made-up")
    (tmp_path / "broken.json").write_text("{not json")
    assert Logins(tmp_path / "broken.json")._ends == {}
    assert json.loads((tmp_path / "l.json").read_text())  # hashes and their ends
