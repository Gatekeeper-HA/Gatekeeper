import pytest

from conftest import write_wav
from gatekeeper.audio import kokoro_lang_code, wav_duration


@pytest.mark.parametrize(
    ("voice", "lang"),
    [
        ("af_heart", "a"),
        ("am_michael", "a"),
        ("bm_george", "b"),
        ("bf_emma", "b"),
        ("", "a"),
    ],
)
def test_kokoro_lang_code_follows_voice_prefix(voice, lang):
    assert kokoro_lang_code(voice) == lang


def test_wav_duration(tmp_path):
    assert wav_duration(write_wav(tmp_path / "a.wav", seconds=1.5)) == pytest.approx(1.5)
    assert wav_duration(tmp_path / "missing.wav") == 3.0


def test_capture_passes_rtsp_timeout_and_survives_a_hang(tmp_path, monkeypatch):
    from gatekeeper import audio

    seen = {}

    def hung(args, timeout):
        # Stopped at the timeout without having written anything.
        seen["args"], seen["timeout"] = args, timeout
        return 255, b"", True

    monkeypatch.setattr(audio, "run_until", hung)
    assert audio.capture_audio_clip("rtsp://cam/x", tmp_path / "c.wav", 4) is None
    args = seen["args"]
    assert args[args.index("-timeout") + 1] == str(audio.RTSP_IO_TIMEOUT_US)
    assert args.index("-timeout") < args.index("-i")
    assert seen["timeout"] == 4 + audio.CAPTURE_GRACE_SECONDS


def test_capture_keeps_what_it_recorded_when_stopped_at_the_timeout(tmp_path, monkeypatch):
    # On an overloaded host ffmpeg can run past the limit; stopping it with
    # SIGTERM lets it finish the WAV, and the visitor's answer may be in it.
    from gatekeeper import audio

    def slow(args, timeout):
        write_wav(tmp_path / "c.wav", seconds=3.0)
        return 255, b"Exiting normally, received signal 15.", True

    monkeypatch.setattr(audio, "run_until", slow)
    assert audio.capture_audio_clip("rtsp://cam/x", tmp_path / "c.wav", 4) == tmp_path / "c.wav"


def test_capture_rejects_failure_and_tiny_output(tmp_path, monkeypatch):
    from gatekeeper import audio

    def failed(args, timeout):
        write_wav(tmp_path / "c.wav", seconds=3.0)
        return 1, b"Connection refused", False

    monkeypatch.setattr(audio, "run_until", failed)
    assert audio.capture_audio_clip("rtsp://cam/x", tmp_path / "c.wav", 4) is None

    def tiny(args, timeout):
        (tmp_path / "c.wav").write_bytes(b"RIFF")
        return 0, b"", False

    monkeypatch.setattr(audio, "run_until", tiny)
    assert audio.capture_audio_clip("rtsp://cam/x", tmp_path / "c.wav", 4) is None


def test_run_until_stops_a_process_that_overruns():
    import sys
    import time

    from gatekeeper.audio import run_until

    start = time.monotonic()
    returncode, _, timed_out = run_until([sys.executable, "-c", "import time; time.sleep(30)"], 0.5)
    assert timed_out
    assert returncode != 0
    assert time.monotonic() - start < 10

    returncode, stderr, timed_out = run_until(
        [sys.executable, "-c", "import sys; sys.stderr.write('done')"], 30
    )
    assert (returncode, stderr, timed_out) == (0, b"done", False)


@pytest.mark.parametrize(
    ("device", "gpus", "compute", "expected"),
    [
        ("auto", 0, "auto", ("cpu", "int8")),
        ("auto", 1, "auto", ("cuda", "float16")),
        ("cpu", 1, "auto", ("cpu", "int8")),
        ("cuda", 0, "int8_float16", ("cuda", "int8_float16")),
        ("auto", 0, "float32", ("cpu", "float32")),
    ],
)
def test_whisper_device_and_compute_type(device, gpus, compute, expected):
    from gatekeeper.audio import whisper_compute_type, whisper_device

    chosen = whisper_device(device, gpus)
    assert (chosen, whisper_compute_type(compute, chosen)) == expected


class FakeWhisper:
    """Stands in for faster_whisper.WhisperModel; ``broken`` devices fail to run."""

    loaded = []
    broken = set()

    def __init__(self, name, device, compute_type):
        FakeWhisper.loaded.append((name, device, compute_type))
        self.device = device

    def transcribe(self, audio, **kw):
        if self.device in FakeWhisper.broken:
            raise RuntimeError("Library libcudnn_ops.so.9 is not found")
        return iter(()), None


@pytest.fixture
def fake_whisper(monkeypatch):
    import sys
    import types

    FakeWhisper.loaded, FakeWhisper.broken = [], set()
    fake = types.SimpleNamespace(WhisperModel=FakeWhisper)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    return FakeWhisper


def test_whisper_uses_the_gpu_when_there_is_one(fake_whisper):
    from gatekeeper.audio import Transcriber

    Transcriber("large-v3-turbo", gpus=lambda: 1).load()
    assert fake_whisper.loaded == [("large-v3-turbo", "cuda", "float16")]


def test_whisper_falls_back_to_the_cpu_when_the_gpu_fails(fake_whisper):
    from gatekeeper.audio import Transcriber

    fake_whisper.broken = {"cuda"}  # e.g. cuDNN missing: only shows up when it runs
    t = Transcriber("small.en", device="cuda")
    t.load()
    assert fake_whisper.loaded == [("small.en", "cuda", "float16"), ("small.en", "cpu", "int8")]
    assert t._model.device == "cpu"
