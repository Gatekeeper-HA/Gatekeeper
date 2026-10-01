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
