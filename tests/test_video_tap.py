import threading

from gatekeeper.video_tap import VideoTap


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Packet:
    def __init__(self, n, keyframe=False):
        self.n, self.is_keyframe, self.size = n, keyframe, 100


class Decoder:
    """Decodes each packet to one frame (its number); one per decoder made."""

    made = 0

    def __init__(self, stream=None):
        Decoder.made += 1
        self.fed = []

    def decode(self, packet):
        self.fed.append(packet.n)
        return [f"frame {packet.n}"]


def tap_with(clock, packets, fps=20, gop=40, seconds=4.0):
    """A tap holding ``packets`` packets that arrived at ``fps``, a keyframe every ``gop``."""
    tap = VideoTap("rtsp://cam/x", seconds=seconds, decoder=Decoder, clock=clock)
    tap._serial = 1
    start = clock.t
    for n in range(packets):
        clock.t = start + n / fps
        tap.add(Packet(n, keyframe=n % gop == 0), n % gop == 0)
    return tap


def take(tap, n, since, every=0.0):
    out = []

    def stop():
        return len(out) >= n

    for at, frame in tap.frames(since, every, stop):
        out.append((round(at, 2), frame))
    return out


def test_only_the_last_seconds_are_kept_from_a_keyframe_on():
    clock = Clock()
    tap = tap_with(clock, 200)  # 10 s at 20 fps, a keyframe every 2 s
    kept = [p.data.n for p in tap._packets]
    # 4 s back is packet 119: kept from the keyframe before it, so it decodes.
    assert kept[0] == 80 and kept[-1] == 199
    assert tap._packets[0].keyframe


def test_frames_start_at_the_keyframe_before_someone_appeared():
    clock = Clock()
    tap = tap_with(clock, 200)
    appeared = 1000.0 + 175 / 20  # packet 175
    frames = take(tap, 3, appeared)
    assert [f for _, f in frames] == ["frame 160", "frame 161", "frame 162"]


def test_frames_are_spaced_by_every():
    clock = Clock()
    tap = tap_with(clock, 200)
    frames = take(tap, 3, 0, every=0.25)  # the oldest kept keyframe, then every 5 packets
    assert [f for _, f in frames] == ["frame 80", "frame 85", "frame 90"]


def test_frames_follow_the_stream_live():
    clock = Clock()
    tap = tap_with(clock, 81)
    got = []
    started = threading.Event()

    def reader():
        for _, frame in tap.frames(clock.t, 0.0, lambda: len(got) >= 4):
            got.append(frame)
            started.set()

    t = threading.Thread(target=reader)
    t.start()
    started.wait(2)
    for n in range(81, 85):
        tap.add(Packet(n), False)
    t.join(2)
    assert not t.is_alive()
    assert got[0] == "frame 80" and got[-1] == "frame 83"


def test_a_reconnect_starts_a_new_decoder_at_the_new_streams_keyframe():
    clock = Clock()
    tap = tap_with(clock, 50)
    before = Decoder.made
    out = []

    def stop():
        if len(out) == 10 and tap._serial == 1:  # frames 40-49 decoded
            # The camera reconnects: the old packets are gone, a new stream starts.
            with tap._cond:
                tap._serial = 2
                tap._packets.clear()
            tap.add(Packet(1000, keyframe=True), True)
            tap.add(Packet(1001), False)
        return len(out) >= 12

    for _, frame in tap.frames(clock.t, 0.0, stop):
        out.append(frame)
    assert out[:2] == ["frame 40", "frame 41"]
    assert out[10:] == ["frame 1000", "frame 1001"]
    assert Decoder.made == before + 2


def test_falling_behind_the_buffer_carries_on_from_the_next_keyframe():
    clock = Clock()
    tap = tap_with(clock, 50, seconds=1.0)
    out = []

    start = clock.t - 49 / 20

    def stop():
        if len(out) == 10:  # frames 40-49 decoded; then 4 s arrive while we're busy
            for n in range(50, 130):
                clock.t = start + n / 20
                tap.add(Packet(n, keyframe=n % 40 == 0), n % 40 == 0)
        return len(out) >= 11

    for _, frame in tap.frames(clock.t - 0.4, 0.0, stop):
        out.append(frame)
    assert out[0] == "frame 40"
    assert out[10] == "frame 80"  # 50-79 were dropped: on from the next keyframe kept


class Container:
    def __init__(self, packets):
        self.streams = type("Streams", (), {"video": ["video stream"]})()
        self._packets = packets
        self.closed = False

    def demux(self, stream):
        yield from self._packets

    def close(self):
        self.closed = True


def test_reading_keeps_the_stream_packets_and_closes_the_connection():
    clock = Clock()
    container = Container([Packet(0, keyframe=True), Packet(1)])
    empty = Packet(2)
    empty.size = 0  # a flush packet
    container._packets.append(empty)
    tap = VideoTap("rtsp://user:pw@cam/x", opener=lambda url: container, clock=clock)
    tap._read_once()
    assert [p.data.n for p in tap._packets] == [0, 1]
    assert tap._serial == 1 and tap._stream == "video stream" and container.closed


def test_packets_come_as_they_came_from_the_keyframe_before_since():
    clock = Clock()
    tap = tap_with(clock, 200)  # 10 s: keyframes at packets 80, 120, 160 still kept
    tap._stream = "stream 1"
    got = []
    for p, stream in tap.packets(clock.t - 2.5, stop=lambda: len(got) >= 50):
        got.append((p.data.n, p.keyframe, stream))
    assert got[0] == (120, True, "stream 1") and [n for n, _, _ in got] == list(range(120, 170))


def test_packets_end_when_the_stream_reconnects():
    clock = Clock()
    tap = tap_with(clock, 60)
    got = []
    for p, _stream in tap.packets(0.0, stop=lambda: False):
        got.append(p.data.n)
        if len(got) == 3:
            tap._serial += 1  # what _read_once does on a new connection
    # Those already taken from the old connection, then it ends (the new one's
    # timestamps start over).
    assert got == list(range(60))
