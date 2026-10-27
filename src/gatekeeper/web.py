"""Gatekeeper's web page (``WEB_PORT``, behind ``WEB_PASSWORD``): the visits at the door
(``/``, visits.py, where the owner says what each visit was) and the household's faces
(``faces/``).

With face recognition on, the faces of people who went in by the front door are kept
for enrollment (face_watch.py). This page shows them grouped by person: tick the faces
that are one person's, choose or type their name, confirm they agreed to be
recognized, and they're enrolled. Faces that aren't the household can be deleted. It
also lists who is enrolled, with forget and purge (face_admin.py does the same from
the command line).

Faces are biometric data:
- the page needs a password (HTTP Basic: any user name, ``WEB_PASSWORD``), and is off
  without one;
- every change needs a token from the page itself, so another site can't make one
  from your browser;
- nothing is cached.

It's plain HTML with relative links, no scripts, so it works on a phone and behind a
proxy that adds a path prefix (Home Assistant's ingress).
"""

from __future__ import annotations

import base64
import datetime
import hmac
import html
import json
import logging
import re
import secrets
import shutil
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from gatekeeper import face_admin
from gatekeeper.faces import Gallery

log = logging.getLogger(__name__)

DEFAULT_PORT = 8098
NAME = re.compile(r"^[\w][\w .'-]{0,39}$")  # letters (any script), digits, space . ' -
CANDIDATE_FILE = re.compile(r"^[\w.-]+\.npz$")
MAX_BODY = 64 * 1024
# A group's name is filled in with the enrolled person it looks most like, when it looks
# this much like them (faces.Gallery.resemblance) and clearly more than like anyone else.
# On the rig's first faces, the right person scored 0.45-0.53 with a margin of 0.08 or
# more; the two wrong guesses, on groups of one or two faces, scored 0.30.
SUGGEST_SCORE = 0.40
SUGGEST_MARGIN = 0.05

STYLE = """
body { font: 16px/1.4 system-ui, sans-serif; margin: 0 auto; max-width: 960px;
  padding: 0 16px 48px; color: #1b1b1f; background: #fafafa; }
h1 { font-size: 1.4em; } h2 { font-size: 1.15em; margin-top: 2em; }
.note { color: #555; } .done { background: #e7f5e9; padding: 8px 12px; border-radius: 6px; }
.error { background: #fdecea; padding: 8px 12px; border-radius: 6px; }
.group { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 12px;
  margin: 12px 0; }
.faces { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.faces label { position: relative; } .faces img { width: 112px; height: 112px;
  border-radius: 6px; display: block; object-fit: cover; }
.faces input { position: absolute; top: 4px; left: 4px; transform: scale(1.3); }
input[type=text] { font-size: 1em; padding: 6px; width: 14em; max-width: 100%; }
button { font-size: 1em; padding: 6px 12px; margin: 4px 4px 4px 0; }
.danger { color: #a40000; }
table { border-collapse: collapse; } td { padding: 4px 12px 4px 0; }
nav { margin: 12px 0 0; } nav a, nav strong { margin-right: 12px; }
.visit { display: flex; gap: 12px; align-items: flex-start; background: #fff;
  border: 1px solid #ddd; border-radius: 8px; padding: 8px; margin: 8px 0; }
.visit img { width: 160px; max-width: 40%; border-radius: 6px; }
.visit form { margin-top: 6px; } select { font-size: 1em; padding: 4px; max-width: 100%; }
@media (prefers-color-scheme: dark) {
  body { background: #17171a; color: #e8e8ea; } .group, .visit { background: #222226;
  border-color: #3a3a40; } .note { color: #aaa; } .done { background: #1f3a24; }
  .error { background: #4a1f1f; } .danger { color: #ff8a80; } }
"""


class FacesPage:
    """What the page shows and does, apart from HTTP (testable on its own)."""

    def __init__(
        self,
        face_dir: Path,
        *,
        enabled: bool,
        candidate_days: float,
        encode: Callable[[np.ndarray], bytes] = face_admin.jpeg_bytes,
        capture: Callable[[], None] | None = None,
        capturing: Callable[[], float] | None = None,
    ) -> None:
        self.face_dir = face_dir
        self.enabled = enabled
        self.candidate_days = candidate_days
        self._encode = encode
        # Capturing faces at the door (face_watch.FaceWatcher), when face recognition runs.
        self._capture = capture
        self._capturing = capturing
        self.token = secrets.token_urlsafe(24)  # proves a form came from this page
        self._lock = threading.Lock()  # one change at a time

    @property
    def gallery(self) -> Gallery:
        return Gallery(self.face_dir / "gallery.json")

    def people(self) -> list[dict]:
        path = self.face_dir / "gallery.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return [
            {"name": name, "faces": len(p["embeddings"]), "added": p.get("added", 0)}
            for name, p in sorted(data.get("people", {}).items())
        ]

    def groups(self) -> list[list[dict]]:
        faces = face_admin.load_candidates(self.face_dir / "candidates")
        if not faces:
            return []
        groups = face_admin.group(
            np.array([f["embedding"] for f in faces]), tracks=[f["file"] for f in faces]
        )
        return [[faces[i] for i in g] for g in groups]

    def suggest(self, faces: list[dict]) -> tuple[str, list[tuple[str, float]]]:
        """The enrolled person a group looks like (or ""), and everyone it resembles,
        most alike first."""
        scores = self.gallery.resemblance([f["embedding"] for f in faces])
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if not ranked:
            return "", []
        best, score = ranked[0]
        margin = score - (ranked[1][1] if len(ranked) > 1 else 0.0)
        sure = score >= SUGGEST_SCORE and margin >= SUGGEST_MARGIN
        return (best if sure else ""), ranked

    def face_jpeg(self, file: str, index: int) -> bytes | None:
        path = self.face_dir / "candidates" / file
        if not CANDIDATE_FILE.match(file) or not path.is_file():
            return None
        faces = [f for f in face_admin.load_candidates(path.parent) if f["file"] == file]
        if not 0 <= index < len(faces):
            return None
        # The sharper picture from the full frame; the aligned 112 px face in older files.
        return faces[index]["display"] or self._encode(faces[index]["face"])

    def enroll(self, name: str, chosen: set[tuple[str, int]], consent: bool) -> str:
        """Enroll the chosen faces as ``name``. Returns what happened, for the page."""
        name = " ".join(name.split())
        if not chosen:
            raise ValueError("Tick at least one face.")
        if not NAME.match(name):
            raise ValueError(
                "A name is 1-40 letters, digits, spaces, dots, hyphens or apostrophes."
            )
        if not consent:
            raise ValueError(
                f"Enroll {name} only if they agreed to be recognized by this doorbell, "
                "and tick the box to say so."
            )
        with self._lock:
            faces = [
                f
                for f in face_admin.load_candidates(self.face_dir / "candidates")
                if (f["file"], f["index"]) in chosen
            ]
            if not faces:
                raise ValueError("Those faces are gone (enrolled or deleted meanwhile).")
            self.gallery.enroll(name, [f["embedding"] for f in faces])
            face_admin.drop(self.face_dir / "candidates", {(f["file"], f["index"]) for f in faces})
        log.info("web: enrolled %d face(s) for %s", len(faces), name)
        return f"Enrolled {len(faces)} face(s) for {name}."

    def discard(self, chosen: set[tuple[str, int]]) -> str:
        if not chosen:
            raise ValueError("Tick at least one face.")
        with self._lock:
            known = {
                (f["file"], f["index"])
                for f in face_admin.load_candidates(self.face_dir / "candidates")
            }
            gone = chosen & known
            if gone:
                face_admin.drop(self.face_dir / "candidates", gone)
        log.info("web: deleted %d kept face(s)", len(gone))
        return f"Deleted {len(gone)} face(s)."

    def capture(self) -> str:
        if self._capture is None:
            raise ValueError("Face recognition is off, so faces can't be captured.")
        self._capture()
        return (
            "Capturing for 2 minutes: walk up to the doorbell and look at it for a few "
            "seconds. Your faces appear here a moment after you step away."
        )

    def forget(self, name: str) -> str:
        with self._lock:
            gone = self.gallery.forget(name)
        if gone:
            log.info("web: forgot %s", name)
        return f"Forgot {name}." if gone else f"{name} isn't enrolled."

    def purge(self, confirm: str) -> str:
        if confirm.strip().lower() != "delete":
            raise ValueError('Type "delete" to delete every face.')
        with self._lock:
            self.gallery.purge()
            shutil.rmtree(self.face_dir / "candidates", ignore_errors=True)
            shutil.rmtree(self.face_dir / "review", ignore_errors=True)
        log.info("web: purged every face")
        return "Deleted every enrolled face and every kept one."

    # -- the page ----------------------------------------------------------------

    def render(self, message: str = "", error: str = "") -> str:
        e = html.escape
        people = self.people()
        names = [p["name"] for p in people]
        out = [
            "<!doctype html><html lang=en><head><meta charset=utf-8>",
            '<meta name=viewport content="width=device-width, initial-scale=1">',
            f"<title>Household faces</title><style>{STYLE}</style></head><body>",
            nav("faces"),
            "<h1>Household faces</h1>",
        ]
        if message:
            out.append(f"<p class=done role=status>{e(message)}</p>")
        if error:
            out.append(f"<p class=error role=alert>{e(error)}</p>")
        if not self.enabled:
            out.append(
                "<p class=note>Face recognition is off (FACE_RECOGNITION). People enrolled "
                "here are recognized once it's on.</p>"
            )
        out.append(
            "<p class=note>People enrolled here aren't greeted at the door; you get a quiet "
            "notification instead. Enroll only people who agreed to be recognized: faces "
            "are biometric data. Faces below are of people who went in by the front door or "
            "came back soon after leaving, or who walked up while capturing, kept "
            f"{self.candidate_days:g} days for you to enroll. Nobody else's face is stored.</p>"
        )
        if self._capture is not None:
            left = self._capturing() if self._capturing else 0
            if left > 0:
                out.append(
                    f"<p class=done>Capturing faces at the door: {left:.0f} s left. "
                    '<a href="./">Reload</a> after stepping away.</p>'
                )
            out.append(
                '<form method=post action="capture"><input type=hidden name=token '
                f'value="{self.token}"><button type=submit>Capture at the door</button> '
                "<span class=note>For someone the camera hasn't caught: tap, then walk up "
                "to the doorbell and look at it for a few seconds.</span></form>"
            )
        out.append("<datalist id=names>")
        out += [f'<option value="{e(n)}">' for n in names]
        out.append("</datalist>")

        groups = self.groups()
        out.append(f"<h2>Faces to enroll ({sum(len(g) for g in groups)})</h2>")
        if not groups:
            out.append(
                "<p class=note>None yet. They come from people who go in by the front door.</p>"
            )
        for n, faces in enumerate(groups, 1):
            visits = len({f["file"] for f in faces})
            days = sorted({_day(f["at"]) for f in faces})
            suggested, ranked = self.suggest(faces)
            if suggested:
                hint = f" Looks like <strong>{e(suggested)}</strong> ({ranked[0][1]:.2f})."
            elif ranked:
                closest = ", ".join(f"{e(n)} {s:.2f}" for n, s in ranked[:2])
                hint = f" <span class=note>Closest: {closest}; not sure.</span>"
            else:
                hint = ""
            out.append(
                f'<form class=group method=post action="enroll"><input type=hidden name=token '
                f'value="{self.token}"><strong>Group {n}</strong>: {len(faces)} face(s), '
                f"{visits} visit(s), {e(', '.join(days))}.{hint}"
                " <span class=note>Untick any face that isn't this person.</span><div class=faces>"
            )
            for f in faces:
                face_id = f"{f['file']}:{f['index']}"
                src = f"face/{urllib.parse.quote(f['file'])}/{f['index']}.jpg"
                out.append(
                    f'<label><input type=checkbox name=face value="{e(face_id)}" checked>'
                    f'<img src="{src}" alt="face {f["index"] + 1}" loading=lazy></label>'
                )
            out.append(
                "</div><label>Name <input type=text name=name list=names required "
                f'maxlength=40 autocomplete=off value="{e(suggested)}"></label> '
                "<label><input type=checkbox name=consent value=yes> They agreed to be "
                "recognized</label><br>"
                "<button type=submit>Enroll the ticked faces</button>"
                '<button type=submit formaction="discard" formnovalidate class=danger>'
                "Not household: delete the ticked faces</button></form>"
            )

        out.append(f"<h2>Enrolled ({len(people)})</h2>")
        if not people:
            out.append("<p class=note>Nobody yet.</p>")
        else:
            out.append("<table>")
            for p in people:
                out.append(
                    f"<tr><td><strong>{e(p['name'])}</strong></td><td>{p['faces']} face(s)</td>"
                    f"<td>since {e(_day(p['added']))}</td><td><form method=post "
                    f'action="forget"><input type=hidden name=token value="{self.token}">'
                    f'<input type=hidden name=name value="{e(p["name"])}">'
                    "<button type=submit class=danger>Forget</button></form></td></tr>"
                )
            out.append("</table>")
        out.append(
            '<h2>Delete everything</h2><form method=post action="purge">'
            f'<input type=hidden name=token value="{self.token}"><label>Type '
            '"delete" <input type=text name=confirm autocomplete=off></label> '
            "<button type=submit class=danger>Delete every face</button></form>"
            "</body></html>"
        )
        return "".join(out)


def _day(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%a %d %b") if ts else "?"


def nav(current: str) -> str:
    """The links between the pages: Visits is at ``./``, Faces at ``faces/`` (relative,
    so they work behind a path prefix)."""
    if current == "faces":
        return '<nav><a href="../">Visits</a> <strong>Faces</strong></nav>'
    return '<nav><strong>Visits</strong> <a href="faces/">Faces</a></nav>'


def _chosen(values: list[str]) -> set[tuple[str, int]]:
    out = set()
    for v in values:
        file, _, index = v.rpartition(":")
        if CANDIDATE_FILE.match(file) and index.isdigit():
            out.add((file, int(index)))
    return out


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def handler(page: FacesPage, password: str, visits=None) -> type[BaseHTTPRequestHandler]:
    """``visits``: the Visits page (visits.VisitsPage) at ``/``, with the faces at
    ``faces/``; without it the faces are at ``/``."""
    expected = password.encode()

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(header[6:]).partition(b":")
                except ValueError:
                    given = b""
                if hmac.compare_digest(given, expected):
                    return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Gatekeeper", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _page(self, status: int = 200, which=None, **kw) -> None:
            body = (which or page).render(**kw).encode()
            self._send(status, body, "text/html; charset=utf-8")

        def do_GET(self) -> None:
            if not self._authorized():
                return
            path = urllib.parse.urlsplit(self.path).path
            jpeg = None
            if m := re.fullmatch(r".*/face/([^/]+)/(\d+)\.jpg", path):
                jpeg = page.face_jpeg(urllib.parse.unquote(m[1]), int(m[2]))
            elif visits and (m := re.fullmatch(r".*/visit/([^/]+)\.jpg", path)):
                jpeg = visits.image(urllib.parse.unquote(m[1]))
            elif path.endswith("/"):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                message = query.get("done", [""])[0][:200]
                if visits and not path.endswith("/faces/"):
                    days = query.get("days", [""])[0]
                    days = min(max(int(days), 1), 30) if days.isdigit() else 3
                    self._page(which=visits, message=message, days=days)
                else:
                    self._page(message=message)
                return
            if jpeg is None:
                self.send_error(404)
            else:
                self._send(200, jpeg, "image/jpeg")

        def do_POST(self) -> None:
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.send_error(413)
                return
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            action = urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[-1]
            which = visits if visits and action == "label" else page
            if not hmac.compare_digest(form.get("token", [""])[0], page.token):
                self._page(
                    403, which, error="That form is out of date: reload the page and try again."
                )
                return
            one = lambda key: form.get(key, [""])[0]  # noqa: E731
            try:
                if which is visits:
                    done = visits.label(one("visit"), one("label"))
                elif action == "enroll":
                    done = page.enroll(
                        one("name"), _chosen(form.get("face", [])), one("consent") == "yes"
                    )
                elif action == "discard":
                    done = page.discard(_chosen(form.get("face", [])))
                elif action == "forget":
                    done = page.forget(one("name"))
                elif action == "capture":
                    done = page.capture()
                elif action == "purge":
                    done = page.purge(one("confirm"))
                else:
                    self.send_error(404)
                    return
            except ValueError as e:
                self._page(400, which, error=str(e))
                return
            # Back to the page (relative, so it works behind a path prefix).
            self.send_response(303)
            self.send_header("Location", "./?" + urllib.parse.urlencode({"done": done}))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            pass  # the actions log themselves; no face paths in the log

    return Handler


def serve(
    page: FacesPage, password: str, port: int = DEFAULT_PORT, visits=None
) -> ThreadingHTTPServer:
    """Serve the pages on ``port`` in a daemon thread."""
    if visits is not None:
        visits.token = page.token  # one token proves a form came from either page
    server = _Server(("0.0.0.0", port), handler(page, password, visits))
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info(
        "%s on :%d", "visits and faces pages" if visits else "faces page", server.server_address[1]
    )
    return server
