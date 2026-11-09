"""The settings page (``settings/`` on the web page): the opt-in features, turned on
without editing .env. Threat levels, the 911 buttons and the address Text 911 sends, and
Halloween.

What it saves is kept in ``LOG_DIR/settings.json`` (mode 600: it holds the address) and
replaces the same settings from the environment (``config.load``). The settings are read
once, at startup, and some are put to use then (Halloween's lines are spoken ahead), so
Gatekeeper restarts to use them: once no visit is running, it stops, and Docker
(``restart: unless-stopped``) or the add-on's supervisor starts it again, in about 30 s.

The not-allowed faces (docs/10) aren't here: they wait for a legal review.
"""

from __future__ import annotations

import html
import json
import logging
from collections.abc import Callable

from pydantic import ValidationError

from gatekeeper.config import PAGE_SETTINGS, SAVED_SETTINGS

log = logging.getLogger(__name__)

# Each choice's words on the page.
THREAT_LEVELS = {
    "off": "Off",
    "shadow": "Shadow: judged and logged only, to see how it would do",
    "on": "On: a suspicious visitor is warned off, with an urgent notification",
}
HALLOWEEN = {
    "auto": "On October 31, between the hours below",
    "on": "On now",
    "off": "Off",
}
TREATS = {
    "unset": "Not said: the costumes are complimented",
    "bowl": "In a bowl by the door",
    "none": "None this year",
}
PRESS_SOUNDS = {"witch": "A witch's cackle", "theremin": "A theremin"}
ADDRESS_LENGTH = 200


class SettingsPage:
    """What the settings page shows and does, apart from HTTP (testable on its own).
    ``settings`` are the ones in use; ``restart()`` puts saved ones to use."""

    def __init__(self, settings, restart: Callable[[], None]) -> None:
        self.settings = settings
        self.file = settings.log_dir / SAVED_SETTINGS
        self._restart = restart
        self.restarting = False
        self.token = ""  # set by web.serve (shared with the faces page)

    def _restart_once(self) -> None:
        if not self.restarting:
            self.restarting = True
            self._restart()

    def save(self, form: dict[str, str]) -> str:
        """The page's form: kept and put to use, or ValueError (nothing kept)."""
        address = " ".join(form.get("home_address", "").split())
        if len(address) > ADDRESS_LENGTH:
            raise ValueError(f"The address is longer than {ADDRESS_LENGTH} characters.")
        chosen = {k: form.get(k, "").strip() for k in PAGE_SETTINGS}
        chosen |= {"emergency_buttons": form.get("emergency_buttons") == "on"}
        chosen |= {"home_address": address}
        for hour in ("halloween_start_hour", "halloween_end_hour"):
            if not chosen[hour].isdigit():
                raise ValueError("Halloween's hours are whole hours, 0 to 24.")
        sound = chosen["halloween_press_sound"]
        if sound not in PRESS_SOUNDS and sound != self.settings.halloween_press_sound:
            raise ValueError("Choose one of the press sounds offered.")
        try:
            # Checked as Gatekeeper will read them: on top of the environment's.
            checked = type(self.settings)(**chosen)
        except ValidationError as e:
            raise ValueError(_said(e)) from None
        values = {k: getattr(checked, k) for k in PAGE_SETTINGS}
        if values == {k: getattr(self.settings, k) for k in PAGE_SETTINGS}:
            return "Nothing changed."
        part = self.file.with_suffix(".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.touch(0o600)
        part.write_text(json.dumps(values, indent=1), encoding="utf-8")
        part.replace(self.file)
        changed = [k for k in PAGE_SETTINGS if values[k] != getattr(self.settings, k)]
        log.info("web: settings saved (%s); restarting to use them", ", ".join(changed) or "-")
        self._restart_once()
        return RESTARTING

    def forget(self) -> str:
        """Back to the environment's settings."""
        if not self.file.exists():
            return "Nothing was saved here."
        self.file.unlink()
        log.info("web: the settings page's settings forgotten; restarting")
        self._restart_once()
        return "Back to .env's settings. " + RESTARTING

    # -- the page ----------------------------------------------------------------

    def render(self, message: str = "", error: str = "") -> str:
        from gatekeeper.web import STYLE, app_head, nav

        e = html.escape
        s = self.settings
        out = [
            "<!doctype html><html lang=en><head><meta charset=utf-8>",
            '<meta name=viewport content="width=device-width, initial-scale=1">',
            f"<title>Settings</title><style>{STYLE}{SETTINGS_STYLE}</style>{app_head('settings')}",
            "</head><body>",
            nav("settings"),
            "<h1>Settings</h1>",
        ]
        if message:
            out.append(f"<p class=done role=status>{e(message)}</p>")
        if error:
            out.append(f"<p class=error role=alert>{e(error)}</p>")
        out.append(
            "<p class=note>Saved here, these replace the same settings in .env. Gatekeeper "
            "restarts to use them: it's back in about 30 seconds, after the visit in "
            "progress if there is one.</p>"
        )
        out.append(
            f'<form method=post action="save"><input type=hidden name=token value="{self.token}">'
        )
        out.append("<h2>Threats and emergencies</h2>")
        out.append(_select("threat_levels", "Threat levels", THREAT_LEVELS, s.threat_levels))
        out.append(
            "<p><label><input type=checkbox name=emergency_buttons value=on"
            f"{' checked' if s.emergency_buttons else ''}> 911 buttons</label><br>"
            "<span class=note>On the notification of a suspicious visitor or an emergency: "
            "Call 911, and Text 911 with your address and what happened (you still tap "
            "send).</span></p>"
        )
        out.append(
            "<p><label>Your address, for Text 911<br>"
            f'<input type=text name=home_address value="{e(s.home_address)}" '
            f"maxlength={ADDRESS_LENGTH} autocomplete=street-address class=wide></label><br>"
            "<span class=note>Without it, only Call 911. It stays on this server.</span></p>"
        )
        out.append("<h2>Halloween</h2>")
        out.append(_select("halloween", "Halloween greetings", HALLOWEEN, s.halloween))
        out.append(
            "<p>From <input type=number name=halloween_start_hour min=0 max=23 "
            f'value="{s.halloween_start_hour}" aria-label="From (hour)"> to '
            "<input type=number name=halloween_end_hour min=1 max=24 "
            f'value="{s.halloween_end_hour}" aria-label="To (hour)"> o\'clock</p>'
        )
        out.append(_select("halloween_treats", "Treats", TREATS, s.halloween_treats))
        sounds = dict(PRESS_SOUNDS)
        if s.halloween_press_sound not in sounds:  # a sound file of the owner's own, in .env
            sounds[s.halloween_press_sound] = f"Your own: {s.halloween_press_sound}"
        out.append(_select("halloween_press_sound", "Press sound", sounds, s.halloween_press_sound))
        out.append("<p><button type=submit>Save and restart</button></p></form>")
        if self.file.exists():
            out.append(
                f'<form method=post action="forget"><input type=hidden name=token '
                f'value="{self.token}"><p class=note>These are saved here. '
                "<button type=submit>Use .env's settings again</button></p></form>"
            )
        out.append("</body></html>")
        return "\n".join(out)


RESTARTING = (
    "Saved. Gatekeeper is restarting to use them: it's back in about 30 seconds, after the "
    "visit in progress if there is one."
)
SETTINGS_STYLE = """
input[type=number] { font-size: 1em; padding: 6px; width: 4em; }
input.wide { width: 28em; }
"""


def _select(name: str, label: str, choices: dict[str, str], current: str) -> str:
    e = html.escape
    options = "".join(
        f'<option value="{e(value)}"{" selected" if value == current else ""}>{e(text)}</option>'
        for value, text in choices.items()
    )
    return f"<p><label>{e(label)}<br><select name={name}>{options}</select></label></p>"


def _said(error: ValidationError) -> str:
    """A setting the page can't save, in words."""
    first = error.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "settings"
    return f"{where}: {first['msg']}".replace("Value error, ", "")
