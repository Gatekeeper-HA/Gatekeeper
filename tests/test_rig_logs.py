"""keep_logs, day_log and kept_logs (scripts/rig/lib.sh), run with a fake docker on the PATH."""

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

# A container's id is in $FAKE/id-<name> (none: no such container). Its log is
# $FAKE/log-<name>, and with --timestamps $FAKE/tslog-<name>.
DOCKER = r"""#!/bin/bash
case "$1" in
  inspect) name=${@: -1}; cat "$FAKE/id-$name" 2>/dev/null || exit 1 ;;
  logs)
    name=${@: -1}
    if [ "$2" = --timestamps ]; then cat "$FAKE/tslog-$name"; else cat "$FAKE/log-$name"; fi ;;
esac
"""


def stamp(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.123456789Z", time.gmtime(time.time() - seconds_ago))


@pytest.fixture
def rig(tmp_path):
    stack = tmp_path / "stack"
    (stack / "scripts" / "rig").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "rig" / "lib.sh", stack / "scripts" / "rig" / "lib.sh")
    (stack / ".env").write_text("SERVER_IP=127.0.0.1\n")
    fake, bin_ = tmp_path / "fake", tmp_path / "bin"
    fake.mkdir()
    bin_.mkdir()
    (bin_ / "docker").write_text(DOCKER)
    (bin_ / "docker").chmod(0o755)

    def run(command: str) -> str:
        env = {
            **os.environ,
            "PATH": f"{bin_}:{os.environ['PATH']}",
            "FAKE": str(fake),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
        script = f'set -euo pipefail; . "{stack}/scripts/rig/lib.sh"; {command}'  # as the scripts
        return subprocess.run(
            [BASH, "-c", script], env=env, check=True, capture_output=True, text=True
        ).stdout

    run.fake, run.logs = fake, tmp_path / "state" / "gatekeeper" / "logs"
    return run


def test_a_replaced_containers_log_is_kept_for_the_day(rig):
    (rig.fake / "id-gatekeeper").write_text("aaaaaaaaaaaa0123\n")
    (rig.fake / "tslog-gatekeeper").write_text(
        f"{stamp(30 * 3600)} too old: before the report's window\n"
        f"{stamp(3600)} 2026-10-23 18:00:00,000 ERROR   gatekeeper.app [-] boom\n"
        f"{stamp(3599)} Traceback (most recent call last):\n"
        f'{stamp(3599)}   File "app.py", line 1, in <module>\n'
    )
    rig("keep_logs")
    assert [p.name for p in rig.logs.iterdir()] == ["gatekeeper-aaaaaaaaaaaa.log"]

    # The deploy recreated Gatekeeper.
    (rig.fake / "id-gatekeeper").write_text("bbbbbbbbbbbb4567\n")
    (rig.fake / "log-gatekeeper").write_text("after the deploy\n")

    assert rig("day_log gatekeeper").splitlines() == [
        "2026-10-23 18:00:00,000 ERROR   gatekeeper.app [-] boom",
        "Traceback (most recent call last):",
        '  File "app.py", line 1, in <module>',
        "after the deploy",
    ]
    assert rig("kept_logs").strip() == '["gatekeeper"]'


def test_a_restart_that_kept_the_container_adds_nothing(rig):
    (rig.fake / "id-gatekeeper").write_text("aaaaaaaaaaaa0123\n")
    (rig.fake / "tslog-gatekeeper").write_text(f"{stamp(60)} saved\n")
    (rig.fake / "log-gatekeeper").write_text("the same container's log\n")
    rig("keep_logs")

    assert rig("day_log gatekeeper").splitlines() == ["the same container's log"]
    assert rig("kept_logs").strip() == "[]"


def test_saved_logs_older_than_two_days_are_dropped(rig):
    rig.logs.mkdir(parents=True)
    old = rig.logs / "gatekeeper-cccccccccccc.log"
    old.write_text("old\n")
    os.utime(old, (time.time() - 3 * 86400,) * 2)
    (rig.fake / "id-gatekeeper").write_text("aaaaaaaaaaaa0123\n")
    (rig.fake / "tslog-gatekeeper").write_text("")
    rig("keep_logs")

    assert [p.name for p in rig.logs.iterdir()] == ["gatekeeper-aaaaaaaaaaaa.log"]
