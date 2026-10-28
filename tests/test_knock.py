import numpy as np

from gatekeeper.knock import KnockListener, Knocks, sharpness

RATE = 16000


def recording(seconds, taps=(), loud=()):
    """Quiet noise, with sharp taps at ``taps`` (seconds) and a loud non-knock at ``loud``."""
    rng = np.random.default_rng(1)
    x = rng.normal(0, 30, int(seconds * RATE))
    for t in taps:
        i = int(t * RATE)
        x[i : i + 40] += rng.normal(0, 8000, 40)  # a short, sharp click
    for t in loud:
        i = int(t * RATE)
        x[i : i + RATE // 2] += rng.normal(0, 6000, RATE // 2)  # a burst of noise (a car)
    return np.clip(x, -32768, 32767).astype(np.int16)


class Tap:
    """The audio tap over a fixed recording: time t is sample t * 16000."""

    def __init__(self, audio, clock):
        self.audio, self.clock = audio, clock

    def slice(self, t0, t1):
        i0, i1 = max(0, int(t0 * RATE)), min(len(self.audio), int(t1 * RATE))
        return self.audio[i0:i1].copy() if i1 > i0 else np.zeros(0, np.int16)

    def wait(self, timeout):
        pass


class Classifier:
    """Says "knock" for a window with taps in it, as AST does; "not" for anything else."""

    def __init__(self, taps):
        self.taps, self.windows = taps, []

    def knock(self, samples):
        self.windows.append(len(samples))
        clicks = int((sharpness(samples) > 0.05).sum())
        return 0.9 if clicks and clicks < 20 else 0.0


def listen(audio, taps, seconds):
    now = [0.0]
    heard = []
    classifier = Classifier(taps)
    listener = KnockListener(
        Tap(audio, lambda: now[0]),
        lambda: classifier,
        on_knock=lambda score, at: heard.append((round(score, 2), round(at, 1))),
        monotonic=lambda: now[0],
        wall=lambda: 1_800_000_000.0 + now[0],
    )
    while now[0] < seconds:
        listener.step()
        now[0] += 0.25
    return heard, listener, classifier


def test_sharpness_is_high_on_a_click():
    audio = recording(1, taps=[0.5])
    frames = sharpness(audio)
    assert len(frames) == 100
    assert frames[50] > 10 * np.median(frames)


def test_a_knock_is_heard_once_and_a_loud_noise_is_not():
    taps = [20.0, 20.3, 20.6, 25.0, 25.3]  # two sets, close: one knock event
    audio = recording(60, taps=taps, loud=[40.0])
    heard, listener, classifier = listen(audio, taps, 60)
    assert heard == [(0.9, 1_800_000_019.7)]  # from just before the first tap
    assert listener.knocks == 1 and listener.checks >= 2  # the car was checked, not a knock
    assert all(n == 2 * RATE for n in classifier.windows)


def test_knocks_far_apart_are_two_events():
    taps = [20.0, 20.3, 40.0, 40.3]
    heard, _, _ = listen(recording(60, taps=taps), taps, 60)
    assert [at for _, at in heard] == [1_800_000_019.7, 1_800_000_039.7]


def test_the_knocks_are_kept(tmp_path):
    knocks = Knocks(tmp_path / "logs" / "knocks.jsonl")
    knocks.add({"at": 100.0, "score": 0.9, "on_camera": False, "in_visit": False})
    knocks.add({"at": 200.0, "score": 0.7, "on_camera": True, "in_visit": True})
    assert [k["at"] for k in knocks.since(150)] == [200.0]
    assert Knocks(tmp_path / "none.jsonl").since(0) == []
