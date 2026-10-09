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


def test_presynth_reuses_a_phrase_whose_text_and_voice_are_unchanged(tmp_path):
    from gatekeeper.audio import presynth_all

    class Synth:
        voice = "af_heart"
        calls = []

        def synthesize(self, text, path):
            self.calls.append(text)
            write_wav(path, seconds=0.5)
            return True

    synth = Synth()
    presynth_all(synth, tmp_path, {"greeting": "Hello.", "civic_thanks": "Thanks."})
    presynth_all(synth, tmp_path, {"greeting": "Hello there.", "civic_thanks": "Thanks."})
    synth.voice = "bm_george"
    done = presynth_all(synth, tmp_path, {"civic_thanks": "Thanks."})
    # First start: both; text changed: only the greeting; voice changed: again.
    assert synth.calls == ["Hello.", "Thanks.", "Hello there.", "Thanks."]
    assert done == {"civic_thanks": tmp_path / "_presynth_civic_thanks.wav"}
