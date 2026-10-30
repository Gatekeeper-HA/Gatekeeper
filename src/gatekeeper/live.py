"""The Live page (``live/`` on the web page): the door now, with sound, in the browser.

The browser and go2rtc talk WebRTC: the browser's offer comes to Gatekeeper (behind the
web page's password, with the page's token), which hands it to go2rtc's API and returns
its answer, so go2rtc's API stays unpublished. Only go2rtc's WebRTC port carries the
video and sound to the browser, on the home network.

The camera's own audio is AAC, which WebRTC can't carry: the live streams in go2rtc copy
the video and make the audio Opus (``ffmpeg:<camera>_sub#video=copy#audio=opus``). The
low-resolution stream is the default: it plays everywhere and is light on a phone; "HD" is
the full-resolution one.

Talking back: the resident's microphone goes to the doorbell's speaker over a second
WebRTC connection, to go2rtc's talk stream (the one Gatekeeper speaks through). Browsers
give the microphone only to an HTTPS page (``WEB_TLS``). The moment they start, Gatekeeper
steps out: the visit it's having ends at once, and it greets nobody while they talk. The
page renews that every ``RENEW_SECONDS`` while they talk; if it stops (the page closed),
Gatekeeper is back ``LEASE_SECONDS`` later.
"""

from __future__ import annotations

import html
import logging
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

MAX_SDP = 64 * 1024
LEASE_SECONDS = 30.0
RENEW_SECONDS = 10

SCRIPT = """
const video = document.getElementById('video');
const state = document.getElementById('state');
let pc = null;
async function start(hd) {
  if (pc) pc.close();
  state.textContent = 'Connecting...';
  pc = new RTCPeerConnection();
  pc.addTransceiver('video', {direction: 'recvonly'});
  pc.addTransceiver('audio', {direction: 'recvonly'});
  const stream = new MediaStream();
  video.srcObject = stream;
  pc.ontrack = e => stream.addTrack(e.track);
  pc.onconnectionstatechange = () => {
    const s = pc.connectionState;
    state.textContent = s === 'connected' ? (hd ? 'Live, HD' : 'Live') :
      s === 'failed' ? 'Could not connect: is go2rtc\\'s port 8555 open on this network?' : s;
  };
  await pc.setLocalDescription(await pc.createOffer());
  // All this side's addresses in the offer: Gatekeeper passes it on in one go.
  await new Promise(done => {
    if (pc.iceGatheringState === 'complete') return done();
    pc.onicegatheringstatechange = () => pc.iceGatheringState === 'complete' && done();
    setTimeout(done, 2000);
  });
  const r = await fetch('offer?hd=' + (hd ? 1 : 0), {
    method: 'POST', body: pc.localDescription.sdp,
    headers: {'Content-Type': 'application/sdp', 'X-Token': document.body.dataset.token},
  });
  if (!r.ok) { state.textContent = 'Could not start: ' + (await r.text()); return; }
  await pc.setRemoteDescription({type: 'answer', sdp: await r.text()});
}
let talk = null, renew = null;
const talkButton = document.getElementById('talk');
function post(path) {
  return fetch(path, {method: 'POST', headers: {'X-Token': document.body.dataset.token}});
}
async function startTalk() {
  if (!window.isSecureContext || !navigator.mediaDevices) {
    state.textContent = 'Talking back needs this page over https:// (WEB_TLS).';
    return;
  }
  let mic;
  try {
    mic = await navigator.mediaDevices.getUserMedia(
      {audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
  } catch (e) { state.textContent = 'No microphone: ' + e.message; return; }
  talk = new RTCPeerConnection();
  for (const track of mic.getAudioTracks()) talk.addTransceiver(track, {direction: 'sendonly'});
  await talk.setLocalDescription(await talk.createOffer());
  await new Promise(done => {
    if (talk.iceGatheringState === 'complete') return done();
    talk.onicegatheringstatechange = () => talk.iceGatheringState === 'complete' && done();
    setTimeout(done, 2000);
  });
  const r = await fetch('talk', {
    method: 'POST', body: talk.localDescription.sdp,
    headers: {'Content-Type': 'application/sdp', 'X-Token': document.body.dataset.token},
  });
  if (!r.ok) { state.textContent = 'Could not talk: ' + (await r.text()); stopTalk(); return; }
  await talk.setRemoteDescription({type: 'answer', sdp: await r.text()});
  renew = setInterval(() => post('talk/alive'), RENEW * 1000);
  talkButton.textContent = 'Stop talking';
  state.textContent = 'Talking: they hear you at the door';
}
function stopTalk() {
  if (renew) clearInterval(renew);
  if (talk) {
    for (const s of talk.getSenders()) if (s.track) s.track.stop();
    talk.close();
    post('talk/end');
  }
  talk = renew = null;
  talkButton.textContent = 'Talk';
}
talkButton.onclick = () => talk ? stopTalk() : startTalk();
window.addEventListener('pagehide', stopTalk);
document.getElementById('sd').onclick = () => start(false);
document.getElementById('hd').onclick = () => start(true);
document.getElementById('sound').onclick = () => { video.muted = !video.muted;
  document.getElementById('sound').textContent = video.muted ? 'Sound on' : 'Sound off'; };
start(false);
"""

LIVE_STYLE = """
video { width: 100%; max-width: 960px; background: #000; border-radius: 6px; display: block; }
"""


class LivePage:
    """The Live page; ``offer(sdp, hd)`` asks go2rtc for the stream and returns its answer;
    ``talk(sdp)`` does the same for the resident's microphone, after Gatekeeper (``owner``:
    the Interaction, with ``step_out`` and ``owner_talks``) steps out."""

    token = ""  # set by web.serve: one token for every page

    def __init__(
        self,
        go2rtc_api: str,
        stream: str,
        hd_stream: str,
        talk_stream: str | None = None,
        owner=None,
        timeout: float = 10.0,
    ) -> None:
        self.go2rtc_api = go2rtc_api.rstrip("/")
        self.stream, self.hd_stream, self.talk_stream = stream, hd_stream, talk_stream
        self.owner = owner
        self.timeout = timeout

    def talk(self, sdp: str) -> str:
        """The resident starts talking: Gatekeeper steps out at once, then go2rtc's answer
        for the doorbell's speaker."""
        if self.talk_stream is None:
            raise ValueError("Talking back isn't set up.")
        if not sdp.startswith("v=0") or len(sdp) > MAX_SDP:
            raise ValueError("That isn't a WebRTC offer.")
        stopped = self.owner.step_out(LEASE_SECONDS) if self.owner is not None else None
        log.info(
            "the resident is talking at the door%s",
            f": visit {stopped} stopped" if stopped else "",
        )
        return self._answer(self.talk_stream, sdp)

    def talking(self, still: bool) -> None:
        """The page renews the resident's talking (``still``), or says they're done."""
        if self.owner is not None:
            self.owner.owner_talks(LEASE_SECONDS if still else 0)
        if not still:
            log.info("the resident stopped talking at the door")

    def offer(self, sdp: str, hd: bool = False) -> str:
        """go2rtc's answer to the browser's offer; ValueError if there isn't one."""
        if not sdp.startswith("v=0") or len(sdp) > MAX_SDP:
            raise ValueError("That isn't a WebRTC offer.")
        answer = self._answer(self.hd_stream if hd else self.stream, sdp)
        log.info("live view started (%s)", "HD" if hd else "low resolution")
        return answer

    def _answer(self, src: str, sdp: str) -> str:
        url = f"{self.go2rtc_api}/api/webrtc?src={urllib.parse.quote(src)}"
        req = urllib.request.Request(
            url, data=sdp.encode(), headers={"Content-Type": "application/sdp"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                answer = resp.read().decode()
        except Exception as e:
            log.warning("live view: go2rtc didn't answer for %s: %s", src, e)
            raise ValueError("The camera's stream isn't available right now.") from None
        return answer

    def render(self, message: str = "", error: str = "") -> str:
        from gatekeeper.web import STYLE, nav

        e = html.escape
        return "\n".join(
            [
                "<!doctype html><html lang=en><head><meta charset=utf-8>",
                '<meta name=viewport content="width=device-width, initial-scale=1">',
                f"<title>Live</title><style>{STYLE}{LIVE_STYLE}</style></head>",
                f'<body data-token="{e(self.token)}">',
                nav("live"),
                "<h1>Live</h1>",
                *([f"<p class=error role=alert>{e(error)}</p>"] if error else []),
                "<video id=video autoplay playsinline muted></video>",
                "<p><span id=state class=note>Starting...</span></p>",
                "<p><button id=talk type=button>Talk</button>"
                "<button id=sound type=button>Sound on</button>"
                "<button id=sd type=button>Normal</button>"
                "<button id=hd type=button>HD</button></p>",
                "<p class=note>The door now, with sound, on your home network. HD is the "
                "camera's full resolution: heavier on a phone. While you talk, Gatekeeper "
                "steps out of any conversation at the door and greets nobody.</p>",
                f"<script>const RENEW = {RENEW_SECONDS};{SCRIPT}</script></body></html>",
            ]
        )
