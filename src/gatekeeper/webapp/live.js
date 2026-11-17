// The Live page (live.py): the door's video and sound over WebRTC, through go2rtc, and
// talking back. The page gives its token, and how often a talk is renewed (RENEW_SECONDS).
const RENEW = Number(document.body.dataset.renew) || 10;
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
      s === 'failed' ? 'Could not connect: is go2rtc\'s port 8555 open on this network?' : s;
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
