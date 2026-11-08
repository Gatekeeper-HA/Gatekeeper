import base64
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import pytest

from gatekeeper import config, web
from gatekeeper.health import Health
from gatekeeper.settings_page import RESTARTING, SettingsPage

PASSWORD = "porch-light"
FORM = {
    "threat_levels": "on",
    "emergency_buttons": "on",
    "home_address": "  12 Elm   Street ",
    "halloween": "auto",
    "halloween_start_hour": "17",
    "halloween_end_hour": "21",
    "halloween_treats": "bowl",
    "halloween_press_sound": "theremin",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("THREAT_LEVELS", "shadow")
    return tmp_path


def page(restarts):
    return SettingsPage(config.load(), restart=lambda: restarts.append(1))


def test_saved_settings_replace_the_environments_after_a_restart(env):
    restarts = []
    p = page(restarts)
    assert p.settings.threat_levels == "shadow" and not p.settings.emergency_buttons

    assert p.save(FORM) == RESTARTING
    assert restarts == [1]
    saved = json.loads((env / "settings.json").read_text())
    assert saved["home_address"] == "12 Elm Street" and saved["emergency_buttons"] is True
    # In use after the restart, over the environment's.
    s = config.load()
    assert (s.threat_levels, s.emergency_buttons, s.home_address) == ("on", True, "12 Elm Street")
    assert (s.halloween_start_hour, s.halloween_end_hour, s.halloween_treats) == (17, 21, "bowl")
    assert s.halloween_press_sound == "theremin"

    # Saved again before the restart: one restart.
    p.save({**FORM, "threat_levels": "off"})
    assert restarts == [1]


def test_nothing_changed_is_no_restart(env):
    restarts = []
    p = page(restarts)
    s = p.settings
    form = {k: str(getattr(s, k)) for k in config.PAGE_SETTINGS}
    form["emergency_buttons"] = "on" if s.emergency_buttons else ""
    assert p.save(form) == "Nothing changed."
    assert restarts == [] and not (env / "settings.json").exists()


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"halloween_start_hour": "22", "halloween_end_hour": "17"}, "HALLOWEEN_START_HOUR"),
        ({"halloween_end_hour": "nine"}, "whole hours"),
        ({"threat_levels": "maximum"}, "threat_levels"),
        ({"halloween_treats": "candy"}, "halloween_treats"),
        ({"halloween_press_sound": "/etc/passwd"}, "press sounds offered"),
        ({"home_address": "x" * 201}, "longer than 200"),
    ],
)
def test_what_cant_be_used_isnt_saved(env, change, said):
    restarts = []
    with pytest.raises(ValueError, match=said):
        page(restarts).save({**FORM, **change})
    assert restarts == [] and not (env / "settings.json").exists()


def test_the_checkbox_off_turns_the_911_buttons_off(env):
    (env / "settings.json").write_text(json.dumps({"emergency_buttons": True}))
    p = page([])
    assert p.settings.emergency_buttons
    p.save({k: v for k, v in FORM.items() if k != "emergency_buttons"})
    assert config.load().emergency_buttons is False


def test_back_to_the_environments(env):
    restarts = []
    p = page(restarts)
    assert p.forget() == "Nothing was saved here." and restarts == []
    p.save(FORM)
    p.restarting = False  # as after the restart
    assert p.forget().startswith("Back to .env's settings.")
    assert restarts == [1, 1] and config.load().threat_levels == "shadow"


def test_a_saved_file_never_stops_gatekeeper(env, caplog):
    (env / "settings.json").write_text("{not json")
    assert config.load().threat_levels == "shadow"
    (env / "settings.json").write_text(json.dumps({"halloween_start_hour": 23}))  # > the end
    assert config.load().halloween_start_hour == 16
    assert "don't fit the environment's" in caplog.text
    # Only the page's own settings are taken from it.
    (env / "settings.json").write_text(json.dumps({"mqtt_host": "elsewhere", "halloween": "on"}))
    s = config.load()
    assert (s.mqtt_host, s.halloween) == ("mqtt", "on")


def test_the_page_shows_whats_in_use(env):
    (env / "settings.json").write_text(json.dumps({"home_address": 'A "quoted" <place>'}))
    p = page([])
    p.token = "T"
    shown = p.render()
    assert '<option value="shadow" selected>' in shown
    assert 'value="A &quot;quoted&quot; &lt;place&gt;"' in shown
    assert '<option value="unset" selected>' in shown  # the treats
    assert "Use .env's settings again" in shown  # something is saved here
    assert 'href="../manifest.webmanifest"' in shown and "<strong>Settings</strong>" in shown


def test_an_own_press_sound_from_env_is_kept(env, monkeypatch):
    monkeypatch.setenv("HALLOWEEN_PRESS_SOUND", "/audio/my-laugh.mp3")
    p = page([])
    assert '<option value="/audio/my-laugh.mp3" selected>' in p.render()
    p.save({**FORM, "halloween_press_sound": "/audio/my-laugh.mp3"})
    assert config.load().halloween_press_sound == "/audio/my-laugh.mp3"


def test_the_restart_waits_for_the_visit_in_progress():
    health = Health(sweep_max_age=10, visit_max_age=90)
    health.visit_started()
    slept, stopped = [], []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 5:
            health.visit_finished()

    health.restart_when_idle("to test", lambda: stopped.append(len(slept)), sleep=sleep)
    assert stopped == [5]
    # Not forever: a visit that never ends (a hang) doesn't keep the old settings.
    health.visit_started()
    stopped.clear()
    health.restart_when_idle("to test", lambda: stopped.append(1), longest=10, sleep=lambda s: None)
    assert stopped == [1]


def test_over_http(env):
    restarts = []
    p = page(restarts)
    faces = web.FacesPage(env / "faces", enabled=True, candidate_days=14)
    server = web.serve(faces, PASSWORD, port=0, settings_page=p)
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    auth = {"Authorization": "Basic " + base64.b64encode(f"a:{PASSWORD}".encode()).decode()}

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def go(path, fields=None, headers=auth):
        data = None if fields is None else urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(base + path, data, headers)
        try:
            with urllib.request.build_opener(NoRedirect).open(req) as r:
                return r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    try:
        assert go("settings/", headers={})[0] == 303  # to the login page
        status, body, _ = go("settings/")
        assert status == 200 and "<h1>Settings</h1>" in body
        token = re.search(r'name=token value="([^"]+)"', body)[1]
        assert token == faces.token  # one token for every page

        assert go("settings/save", {**FORM, "token": "stale"})[0] == 403
        status, body, _ = go("settings/save", {**FORM, "halloween_end_hour": "x", "token": token})
        assert status == 400 and "whole hours" in body and restarts == []

        status, _, headers = go("settings/save", {**FORM, "token": token})
        assert status == 303 and headers["Location"].startswith("./?done=Saved.")
        assert restarts == [1]
        status, body, _ = go("settings/?done=" + urllib.parse.quote(RESTARTING))
        assert html.escape(RESTARTING) in body
        assert "Settings" in go("")[1]  # in every page's menu
    finally:
        server.shutdown()
