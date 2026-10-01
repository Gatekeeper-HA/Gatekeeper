"""Playing audio through the camera speaker via a go2rtc WebRTC backchannel."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

try:
    import aiohttp
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.contrib.media import MediaPlayer

    HAS_WEBRTC = True
except ImportError:
    HAS_WEBRTC = False

log = logging.getLogger(__name__)


async def play_wav(go2rtc_api: str, talk_stream: str, wav_path: Path) -> RTCPeerConnection | None:
    """Open a WebRTC session to go2rtc's talk stream and start playing ``wav_path``.

    Returns the connected peer connection, or None on failure. Audio starts
    flowing as soon as this returns; the caller keeps the connection open for
    the clip's duration plus a buffer, then closes it.
    """
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
    player = MediaPlayer(str(wav_path))

    if player.audio is None:
        log.error("no audio track in %s", wav_path.name)
        await pc.close()
        return None

    pc.addTrack(player.audio)
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    for _ in range(50):
        if pc.iceGatheringState == "complete":
            break
        await asyncio.sleep(0.1)

    url = f"{go2rtc_api.rstrip('/')}/api/webrtc?src={talk_stream}"
    log.info("talkback → %s", url)
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                url,
                data=pc.localDescription.sdp,
                headers={"Content-Type": "application/sdp"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 400:
                    log.error("go2rtc %s: %s", resp.status, await resp.text())
                    await pc.close()
                    return None
                answer_sdp = await resp.text()
    except Exception as e:
        log.error("talkback signaling failed: %s", e)
        await pc.close()
        return None

    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

    for _ in range(100):
        if pc.connectionState in ("connected", "failed", "closed"):
            break
        await asyncio.sleep(0.1)

    if pc.connectionState != "connected":
        log.error("talkback never connected: %s", pc.connectionState)
        await pc.close()
        return None

    log.info("talkback connected, playing %s", wav_path.name)
    return pc
