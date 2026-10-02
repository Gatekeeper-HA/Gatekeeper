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
    import subprocess

    from gatekeeper import audio

    seen = {}

    def hung(args, input_bytes=None, timeout=None):
        seen["args"], seen["timeout"] = args, timeout
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(audio, "run_cmd", hung)
    assert audio.capture_audio_clip("rtsp://cam/x", tmp_path / "c.wav", 4) is None
    args = seen["args"]
    assert args[args.index("-timeout") + 1] == str(audio.RTSP_IO_TIMEOUT_US)
    assert args.index("-timeout") < args.index("-i")
    assert seen["timeout"] == 4 + audio.CAPTURE_GRACE_SECONDS


def test_capture_rejects_tiny_output(tmp_path, monkeypatch):
    import subprocess

    from gatekeeper import audio

    def tiny(args, input_bytes=None, timeout=None):
        (tmp_path / "c.wav").write_bytes(b"RIFF")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(audio, "run_cmd", tiny)
    assert audio.capture_audio_clip("rtsp://cam/x", tmp_path / "c.wav", 4) is None
