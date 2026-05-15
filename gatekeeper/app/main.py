import asyncio
import json
import os
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import paho.mqtt.client as mqtt
import soundfile as sf
from faster_whisper import WhisperModel
try:
    from kokoro import KPipeline
    HAS_KOKORO = True
except ImportError:
    HAS_KOKORO = False

try:
    import aiohttp
    from aiortc import (
        RTCConfiguration,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.contrib.media import MediaPlayer
    HAS_WEBRTC = True
except ImportError:
    HAS_WEBRTC = False


MQTT_HOST = os.getenv("MQTT_HOST", "mqtt")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "frigate/events")

CAMERA_NAME = os.getenv("CAMERA_NAME", "front_door")
GO2RTC_API = os.getenv("GO2RTC_API", "http://go2rtc:1984")
AUDIO_RTSP_URL = os.getenv("AUDIO_RTSP_URL", f"rtsp://go2rtc:8554/{CAMERA_NAME}")

DWELL_SECONDS = int(os.getenv("DWELL_SECONDS", "1"))
LISTEN_SECONDS = int(os.getenv("LISTEN_SECONDS", "4"))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "120"))
SWEEP_INTERVAL_SECONDS = float(os.getenv("SWEEP_INTERVAL_SECONDS", "1.0"))

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "tiny")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

KOKORO_VOICE = os.getenv("KOKORO_VOICE", "af_heart")

AUDIO_DIR = Path(os.getenv("AUDIO_DIR", "/audio"))
LOG_DIR = Path(os.getenv("LOG_DIR", "/logs"))

GO2RTC_TALK_STREAM = os.getenv("GO2RTC_TALK_STREAM", "front_door_talk")

IN_DIR = AUDIO_DIR / "in"
OUT_DIR = AUDIO_DIR / "out"
LOG_FILE = LOG_DIR / "events.jsonl"

SESSIONS: Dict[str, Dict] = {}
LOCK = threading.Lock()
PRESYNTH: Dict[str, Path] = {}

# go2rtc supports one talkback WebRTC session at a time.
# This lock prevents two interactions from racing for the same stream.
TALKBACK_LOCK = threading.Lock()

DELIVERY_WORDS = {
    "delivery", "deliver", "fedex", "ups", "amazon", "package",
    "doordash", "uber eats", "ubereats", "instacart",
    "entrega", "paquete",
}

SALES_WORDS = {
    "sales", "selling", "soliciting", "solicitation", "canvassing",
    "campaign", "petition", "ventas", "vender",
}

MAINTENANCE_WORDS = {
    "maintenance", "repair", "service", "technician", "tech",
    "contractor", "landscaping", "plumber", "electrician",
    "mantenimiento", "servicio", "tecnico", "técnico",
}

NO_ANSWER_RESPONSE = "You are being recorded. Please state your purpose or leave the property."
DELIVERY_RESPONSE = "Thank you. Please leave the package at the door."
SALES_RESPONSE = "No solicitation. Please leave the property."
MAINTENANCE_RESPONSE = "Please wait while I notify the resident."
GENERIC_RESPONSE = "Thank you. Please wait while I notify the resident."
GREETING = os.getenv("GREETING", "Hello. This property is monitored. Please state the purpose of your visit.")

RESPONSE_TO_PRESYNTH_KEY = {
    NO_ANSWER_RESPONSE: "no_answer",
    DELIVERY_RESPONSE: "delivery",
    SALES_RESPONSE: "sales",
    MAINTENANCE_RESPONSE: "maintenance",
    GENERIC_RESPONSE: "generic",
}


def ensure_dirs() -> None:
    IN_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def presynth_all() -> None:
    """Pre-synthesize all fixed responses at startup so playback is instant."""
    items = {
        "greeting": GREETING,
        "no_answer": NO_ANSWER_RESPONSE,
        "delivery": DELIVERY_RESPONSE,
        "sales": SALES_RESPONSE,
        "maintenance": MAINTENANCE_RESPONSE,
        "generic": GENERIC_RESPONSE,
    }
    for key, text in items.items():
        path = OUT_DIR / f"_presynth_{key}.wav"
        if synthesize(text, path):
            PRESYNTH[key] = path
            print(f"[presynth] {key}: ok")
        else:
            print(f"[presynth] FAILED: {key}")


def log_event(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_cmd(args, input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def synthesize(text: str, wav_path: Path) -> bool:
    if not HAS_KOKORO:
        print("[synth] kokoro not available")
        return False
    try:
        try:
            pipeline = synthesize._pipeline
        except AttributeError:
            print(f"[synth] loading Kokoro pipeline (voice={KOKORO_VOICE})")
            synthesize._pipeline = KPipeline(lang_code="a")
            pipeline = synthesize._pipeline

        chunks = [audio for _, _, audio in pipeline(text, voice=KOKORO_VOICE, speed=1.0)]
        if not chunks:
            print("[synth] no audio generated")
            return False

        audio = np.concatenate(chunks)
        sf.write(str(wav_path), audio, 24000, subtype="PCM_16")
        return True
    except Exception as e:
        print(f"[synth] failed: {e}")
        return False


def _wav_duration(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path), "rb") as wf:
            rate = wf.getframerate()
            return wf.getnframes() / float(rate) if rate else 0.0
    except Exception:
        return 3.0


async def _webrtc_connect(wav_path: Path):
    """
    Establish a WebRTC connection to go2rtc and start playing wav_path.
    Returns the connected RTCPeerConnection on success, or None on failure.
    Audio starts flowing as soon as this returns — the caller should sleep
    for _wav_duration(wav_path) + buffer before closing the PC.
    """
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
    player = MediaPlayer(str(wav_path))

    if player.audio is None:
        print("[talkback] no audio track in WAV")
        await pc.close()
        return None

    pc.addTrack(player.audio)
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    for _ in range(50):
        if pc.iceGatheringState == "complete":
            break
        await asyncio.sleep(0.1)

    url = f"{GO2RTC_API.rstrip('/')}/api/webrtc?src={GO2RTC_TALK_STREAM}"
    print(f"[talkback] → {url}")
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                url,
                data=pc.localDescription.sdp,
                headers={"Content-Type": "application/sdp"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 400:
                    print(f"[talkback] go2rtc {resp.status}: {await resp.text()}")
                    await pc.close()
                    return None
                answer_sdp = await resp.text()
    except Exception as e:
        print(f"[talkback] signaling failed: {e}")
        await pc.close()
        return None

    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

    for _ in range(100):
        if pc.connectionState in ("connected", "failed", "closed"):
            break
        await asyncio.sleep(0.1)

    if pc.connectionState != "connected":
        print(f"[talkback] never connected: {pc.connectionState}")
        await pc.close()
        return None

    print(f"[talkback] connected, playing {wav_path.name}")
    return pc


async def _interact_async(event_id: str) -> None:
    if not HAS_WEBRTC:
        print("[talkback] aiortc not available")
        return

    loop = asyncio.get_running_loop()

    # ── Greeting ──────────────────────────────────────────────────────────────
    greet_wav = PRESYNTH.get("greeting")
    if greet_wav is None:
        greet_wav = OUT_DIR / f"{event_id}_greeting.wav"
        await loop.run_in_executor(None, synthesize, GREETING, greet_wav)

    greet_pc = await _webrtc_connect(greet_wav)
    if greet_pc is None:
        return

    # Audio starts flowing as soon as _webrtc_connect returns. Sleep for the
    # full clip duration + 2.5s buffer so the camera has time to play everything
    # (covers go2rtc's jitter buffer + RTSP backchannel latency + camera buffer).
    greet_duration = _wav_duration(greet_wav)
    await asyncio.sleep(greet_duration + 2.5)
    await greet_pc.close()

    # ── Listen ────────────────────────────────────────────────────────────────
    # Wait for room echo to fully die before recording.
    await asyncio.sleep(0.5)

    clip_path = await loop.run_in_executor(
        None, capture_audio_clip, event_id, LISTEN_SECONDS
    )
    print(f"[{event_id}] capture: {clip_path} ({clip_path.stat().st_size if clip_path else 0} bytes)")

    transcript = ""
    classification = "unknown_uncooperative"
    response_text = NO_ANSWER_RESPONSE

    if clip_path:
        transcript = await loop.run_in_executor(None, transcribe_audio, clip_path)
        print(f"[{event_id}] transcript: {transcript!r}")
        classification, response_text = classify_response(transcript)

    # ── Reply ─────────────────────────────────────────────────────────────────
    presynth_key = RESPONSE_TO_PRESYNTH_KEY.get(response_text)
    if presynth_key and presynth_key in PRESYNTH:
        reply_wav = PRESYNTH[presynth_key]
    else:
        reply_wav = OUT_DIR / f"{event_id}_reply.wav"
        await loop.run_in_executor(None, synthesize, response_text, reply_wav)

    reply_pc = await _webrtc_connect(reply_wav)
    if reply_pc is not None:
        reply_duration = _wav_duration(reply_wav)
        await asyncio.sleep(reply_duration + 2.5)
        await reply_pc.close()

    result = {
        "ts": time.time(),
        "event_id": event_id,
        "camera": CAMERA_NAME,
        "classification": classification,
        "transcript": transcript,
        "response": response_text,
    }
    log_event(result)
    print(f"[{event_id}] {json.dumps(result, ensure_ascii=False)}")


def interact(event_id: str) -> None:
    print(f"[{event_id}] interaction started")
    if not TALKBACK_LOCK.acquire(blocking=False):
        print(f"[{event_id}] talkback busy, skipping")
        return
    try:
        asyncio.run(_interact_async(event_id))
    except Exception as e:
        print(f"[{event_id}] interaction error: {e}")
    finally:
        TALKBACK_LOCK.release()


def capture_audio_clip(event_id: str, seconds: int) -> Optional[Path]:
    wav_path = IN_DIR / f"{event_id}.wav"
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", AUDIO_RTSP_URL,
        "-vn",
        "-map", "0:a:0?",
        "-ac", "1",
        "-ar", "16000",
        "-t", str(seconds),
        "-y",
        str(wav_path),
    ]
    proc = run_cmd(cmd)
    if proc.returncode != 0 or not wav_path.exists():
        print("Audio capture failed:", proc.stderr.decode("utf-8", errors="ignore"))
        return None

    try:
        if wav_path.stat().st_size < 1024:
            print("Audio capture too small or empty")
            return None
    except Exception:
        return None

    return wav_path


def transcribe_audio(wav_path: Path) -> str:
    try:
        model = transcribe_audio.whisper_model
    except AttributeError:
        print(f"Loading Whisper model: {WHISPER_MODEL} ({WHISPER_COMPUTE_TYPE})")
        transcribe_audio.whisper_model = WhisperModel(
            WHISPER_MODEL,
            device="cpu",
            compute_type=WHISPER_COMPUTE_TYPE,
        )
        model = transcribe_audio.whisper_model

    try:
        segments, _info = model.transcribe(
            str(wav_path),
            beam_size=1,
            language="en",
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
    except Exception as e:
        print("Transcription error:", e)
        return ""


def classify_response(text: str) -> tuple[str, str]:
    normalized = text.lower().strip()

    if not normalized:
        return "unknown_uncooperative", NO_ANSWER_RESPONSE

    if any(word in normalized for word in DELIVERY_WORDS):
        return "likely_delivery", DELIVERY_RESPONSE

    if any(word in normalized for word in SALES_WORDS):
        return "unknown_cooperative", SALES_RESPONSE

    if any(word in normalized for word in MAINTENANCE_WORDS):
        return "unknown_cooperative", MAINTENANCE_RESPONSE

    if len(normalized.split()) <= 1:
        return "unknown_uncooperative", NO_ANSWER_RESPONSE

    return "unknown_cooperative", GENERIC_RESPONSE


def maybe_handle_person_event(event_id: str) -> None:
    with LOCK:
        session = SESSIONS.get(event_id)
        if not session:
            return

        if session.get("handled") or session.get("in_progress"):
            return

        age = time.time() - session["first_seen"]
        if age < DWELL_SECONDS:
            return

        session["in_progress"] = True

    def _worker():
        try:
            interact(event_id)
        finally:
            with LOCK:
                if event_id in SESSIONS:
                    SESSIONS[event_id]["handled"] = True
                    SESSIONS[event_id]["in_progress"] = False

    threading.Thread(target=_worker, daemon=True).start()


def sweep_sessions_loop() -> None:
    while True:
        try:
            now = time.time()
            to_check = []
            to_delete = []

            with LOCK:
                for event_id, session in list(SESSIONS.items()):
                    last_seen = session.get("last_seen", session.get("first_seen", now))
                    if (now - last_seen) > SESSION_TTL_SECONDS:
                        to_delete.append(event_id)
                    else:
                        to_check.append(event_id)

                for event_id in to_delete:
                    SESSIONS.pop(event_id, None)

            for event_id in to_check:
                maybe_handle_person_event(event_id)

        except Exception as e:
            print("Session sweep error:", e)

        time.sleep(SWEEP_INTERVAL_SECONDS)


def on_connect(client, userdata, flags, reason_code, properties=None):
    rc = getattr(reason_code, "value", reason_code)
    print(f"Connected to MQTT rc={rc}")
    client.subscribe(MQTT_TOPIC)


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8", errors="ignore"))
    except Exception:
        return

    event_type = payload.get("type", "")
    after = payload.get("after") or {}
    before = payload.get("before") or {}
    event = after if after else before

    if not event:
        return

    camera = event.get("camera")
    label = event.get("label")
    event_id = event.get("id")

    if camera != CAMERA_NAME or label != "person" or not event_id:
        return

    if event_type in {"new", "update"}:
        now = time.time()
        with LOCK:
            if event_id not in SESSIONS:
                SESSIONS[event_id] = {
                    "first_seen": now,
                    "last_seen": now,
                    "handled": False,
                    "in_progress": False,
                }
            else:
                SESSIONS[event_id]["last_seen"] = now

        maybe_handle_person_event(event_id)

    elif event_type == "end":
        with LOCK:
            SESSIONS.pop(event_id, None)


def build_mqtt_client() -> mqtt.Client:
    try:
        return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    except Exception:
        return mqtt.Client()


def main():
    ensure_dirs()
    presynth_all()

    threading.Thread(target=sweep_sessions_loop, daemon=True).start()

    client = build_mqtt_client()
    client.on_connect = on_connect
    client.on_message = on_message

    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            break
        except Exception as e:
            print("MQTT connect failed, retrying:", e)
            time.sleep(5)

    client.loop_forever()


if __name__ == "__main__":
    main()
