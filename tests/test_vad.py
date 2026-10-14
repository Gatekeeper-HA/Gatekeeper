import numpy as np
import pytest

from gatekeeper.vad import CHUNK_SAMPLES, CHUNK_SECONDS, CONTEXT_SAMPLES, Endpointer, StreamingVAD

C = CHUNK_SECONDS


def run(ep: Endpointer, probs):
    """Feed one probability per chunk from t=0; return the TurnEnd (or None)."""
    for i, p in enumerate(probs):
        end = ep.update((i + 1) * C, p)
        if end:
            return end
    return None


def chunks(seconds: float, prob: float) -> list[float]:
    return [prob] * round(seconds / C)


def endpointer(**kw):
    kw.setdefault("listen_from", 1.0)
    kw.setdefault("no_input_by", 5.0)
    return Endpointer(**kw)


def test_answer_then_silence_ends_the_turn():
    probs = chunks(1.5, 0.0) + chunks(1.0, 0.9) + chunks(2.0, 0.0)
    end = run(endpointer(end_silence=0.7), probs)
    assert end.reason == "answered"
    assert end.speech_start == pytest.approx(1.5, abs=C)
    assert end.speech_end == pytest.approx(2.5, abs=C)
    assert end.at == pytest.approx(2.5 + 0.7, abs=2 * C)


def test_no_answer_ends_at_the_no_input_deadline():
    end = run(endpointer(no_input_by=5.0), chunks(8.0, 0.1))
    assert end.reason == "no_input"
    assert end.at == pytest.approx(5.0, abs=C)
    assert end.speech_start is None and end.speech_end is None


def test_speech_before_listen_from_is_the_prompt_echo_and_does_not_count():
    probs = chunks(0.9, 0.9) + chunks(6.0, 0.0)
    end = run(endpointer(listen_from=1.0, no_input_by=5.0), probs)
    assert end.reason == "no_input"


def test_answer_over_the_end_of_the_echo_counts_from_listen_from():
    # The visitor starts talking before the echo has ended and keeps going.
    probs = chunks(0.6, 0.0) + chunks(1.4, 0.9) + chunks(1.5, 0.0)
    end = run(endpointer(listen_from=1.0), probs)
    assert end.reason == "answered"
    assert end.speech_start == pytest.approx(1.0, abs=C)


def test_hysteresis_keeps_a_soft_syllable_inside_the_answer():
    # 0.4 is below the start threshold but above the end threshold (0.35).
    probs = chunks(1.5, 0.0) + chunks(0.5, 0.9) + chunks(0.8, 0.4) + chunks(0.5, 0.9)
    probs += chunks(2.0, 0.0)
    end = run(endpointer(end_silence=0.7), probs)
    assert end.reason == "answered"
    assert end.speech_end == pytest.approx(1.5 + 0.5 + 0.8 + 0.5, abs=C)


def test_a_short_blip_is_not_an_answer():
    probs = chunks(2.0, 0.0) + chunks(0.1, 0.9) + chunks(5.0, 0.0)
    end = run(endpointer(min_speech=0.25), probs)
    assert end.reason == "no_input"


def test_speech_still_going_at_the_no_input_deadline_waits_for_its_end():
    probs = chunks(4.5, 0.0) + chunks(2.0, 0.9) + chunks(1.0, 0.0)
    end = run(endpointer(no_input_by=5.0), probs)
    assert end.reason == "answered"
    assert end.at > 6.5


def test_a_long_answer_is_cut_at_max_answer():
    end = run(endpointer(max_answer=3.0), chunks(1.5, 0.0) + chunks(10.0, 0.9))
    assert end.reason == "max"
    assert end.at == pytest.approx(1.5 + 3.0, abs=2 * C)


class FakeSession:
    """Stands in for Silero's ONNX session: the probability is the chunk's peak, and
    it records the inputs and state it was given."""

    def __init__(self):
        self.inputs = []

    def run(self, _outputs, feeds):
        x = feeds["input"]
        self.inputs.append((x.copy(), feeds["h"].copy()))
        prob = float(np.abs(x[0, CONTEXT_SAMPLES:]).max())
        return np.array([[prob]], dtype=np.float32), feeds["h"] + 1, feeds["c"]


def test_streaming_vad_scores_whole_chunks_across_uneven_feeds():
    session = FakeSession()
    vad = StreamingVAD(session)
    audio = np.zeros(CHUNK_SAMPLES * 3, dtype=np.int16)
    audio[CHUNK_SAMPLES + 10] = 16384  # loud sample in the second chunk

    probs = vad.feed(audio[:700]) + vad.feed(audio[700:1100]) + vad.feed(audio[1100:])

    assert probs == pytest.approx([0.0, 0.5, 0.0])
    # Each chunk carries the previous chunk's last 64 samples and the model state.
    second_input, second_h = session.inputs[1]
    assert second_input.shape == (1, CONTEXT_SAMPLES + CHUNK_SAMPLES)
    assert second_h[0, 0, 0] == 1
    assert session.inputs[2][0][0, CONTEXT_SAMPLES - 1] == 0  # context: chunk 2's tail


def test_streaming_vad_keeps_leftover_samples_for_the_next_feed():
    vad = StreamingVAD(FakeSession())
    assert vad.feed(np.zeros(CHUNK_SAMPLES - 1, dtype=np.int16)) == []
    assert len(vad.feed(np.zeros(1, dtype=np.int16))) == 1


def test_speech_seconds_counts_speech_only_inside_the_span(tmp_path):
    import wave

    from gatekeeper.vad import SAMPLE_RATE, speech_seconds

    audio = np.zeros(SAMPLE_RATE * 3, dtype=np.int16)
    audio[SAMPLE_RATE : 2 * SAMPLE_RATE] = 20000  # "speech" from 1 s to 2 s
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())

    def spoken(start, end):
        return speech_seconds(path, start, end, vad=StreamingVAD(FakeSession()))

    assert spoken(0.0, 3.0) == pytest.approx(1.0, abs=2 * CHUNK_SECONDS)
    assert spoken(2.2, 3.0) == 0.0
    assert spoken(0.0, 0.9) == 0.0
