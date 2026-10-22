"""scripts/rig/audio-check.sh, run with fake docker, flock, curl and sleep on the PATH."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(
    BASH is None or os.name == "nt", reason="needs a POSIX bash with GNU date and stat"
)

FAKES = {
    # ffprobe's audio packet count: 156 with audio, 1 without. A visit runs while
    # $FAKE/visits counts down. Restarting Frigate brings the audio back if
    # $FAKE/restart-fixes exists.
    "docker": r"""#!/bin/bash
echo "docker $*" >> "$FAKE/calls"
case "$1" in
  run) [ -f "$FAKE/audio" ] && echo 156 || echo 1 ;;
  inspect) case "$*" in *Config.Image*) echo gatekeeper:local ;; *) echo healthy ;; esac ;;
  exec)
    n=$(cat "$FAKE/visits" 2>/dev/null || echo 0)
    if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$FAKE/visits"; echo True; else echo False; fi ;;
  compose) [ -f "$FAKE/restart-fixes" ] && touch "$FAKE/audio" ;;
esac
exit 0
""",
    "flock": '#!/bin/bash\necho "flock $*" >> "$FAKE/calls"\n',
    "sleep": '#!/bin/bash\necho "sleep $*" >> "$FAKE/calls"\n',
    "curl": r"""#!/bin/bash
for a in "$@"; do case "$a" in Title:*) echo "notify ${a#Title: }" >> "$FAKE/calls" ;; esac; done
""",
}


@pytest.fixture
def rig(tmp_path):
    stack = tmp_path / "stack"
    (stack / "scripts" / "rig").mkdir(parents=True)
    for name in ("audio-check.sh", "lib.sh"):
        shutil.copy(REPO / "scripts" / "rig" / name, stack / "scripts" / "rig" / name)
    data = tmp_path / "data"
    (stack / ".env").write_text(f"SERVER_IP=127.0.0.1\nDATA_DIR={data}\nNTFY_GATEKEEPER_TOKEN=t\n")
    hour = time.strftime("%Y-%m-%d/%H", time.gmtime())
    segments = data / "frigate" / "storage" / "recordings" / hour / "front_door"
    segments.mkdir(parents=True)
    for k in range(4):
        seg = segments / f"00.{k}0.mp4"
        seg.write_bytes(b"")
        os.utime(seg, (time.time() - 40 + 10 * k,) * 2)
    fake, bin_ = tmp_path / "fake", tmp_path / "bin"
    fake.mkdir()
    bin_.mkdir()
    for name, body in FAKES.items():
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    state = tmp_path / "state"

    def run():
        (fake / "calls").write_text("")
        env = {
            **os.environ,
            "PATH": f"{bin_}:{os.environ['PATH']}",
            "FAKE": str(fake),
            "XDG_STATE_HOME": str(state),
        }
        subprocess.run(
            [BASH, str(stack / "scripts" / "rig" / "audio-check.sh")], env=env, check=True
        )
        return (fake / "calls").read_text().splitlines()

    run.fake, run.state = fake, state / "gatekeeper"
    return run


def restarts(calls):
    return [c for c in calls if c == "docker compose restart frigate"]


def notes(calls):
    return [c.removeprefix("notify ") for c in calls if c.startswith("notify ")]


def test_recordings_with_audio_are_left_alone(rig):
    (rig.fake / "audio").touch()
    calls = rig()
    assert restarts(calls) == [] and notes(calls) == []


def test_silent_recordings_restart_frigate_and_say_so_quietly(rig):
    (rig.fake / "restart-fixes").touch()
    calls = rig()
    assert restarts(calls) == ["docker compose restart frigate"]
    assert "flock -w 900 9" in calls  # not during a deploy or the report
    assert notes(calls) == ["Restarted Frigate: recordings had no audio"]
    assert (rig.state / "audio-restart").exists()


def test_a_restart_that_doesnt_help_alerts(rig):
    calls = rig()
    assert restarts(calls) == ["docker compose restart frigate"]
    assert notes(calls) == ["Frigate recordings have no audio"]


def test_frigate_is_restarted_at_most_every_6_hours_and_alerts_too(rig):
    rig()  # restarted, didn't help, alerted
    calls = rig()  # an hour later, still silent
    assert restarts(calls) == [] and notes(calls) == []
    for name in ("audio-restart", "audio-alert"):
        old = time.time() - 7 * 3600
        os.utime(rig.state / name, (old, old))
    (rig.fake / "restart-fixes").touch()
    assert restarts(rig()) == ["docker compose restart frigate"]


def test_a_visit_in_progress_is_waited_out(rig):
    (rig.fake / "visits").write_text("2")
    (rig.fake / "restart-fixes").touch()
    calls = rig()
    restart = calls.index("docker compose restart frigate")
    assert calls[:restart].count("sleep 10") == 2
