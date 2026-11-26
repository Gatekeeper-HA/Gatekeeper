"""deploy.sh's record of a deploy (scripts/rig/deploy.sh), on a real git history."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASH, GIT, JQ = shutil.which("bash"), shutil.which("git"), shutil.which("jq")
pytestmark = pytest.mark.skipif(
    None in (BASH, GIT, JQ) or os.name == "nt", reason="needs bash, git and jq"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_a_deploy_records_every_pull_request_it_takes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "What was running (#1)")
    current = git(repo, "rev-parse", "HEAD")
    for subject in [
        "Bump go2rtc from 1.9.14 to 1.9.15 (#207)",
        "A direct push, no pull request",
        "Daily report: say which visits (#195) (#196)",
    ]:
        git(repo, "commit", "-q", "--allow-empty", "-m", subject)
    target = git(repo, "rev-parse", "HEAD")

    # deploy.sh runs main when sourced; take its functions without it.
    script = (REPO / "scripts" / "rig" / "deploy.sh").read_text()
    functions = script.split('main "$@"')[0]
    state = tmp_path / "state"
    state.mkdir()
    subprocess.run(
        [BASH, "-c", functions + 'record deployed ""'],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "state": str(state),
            "current": current,
            "target": target,
            "subject": "Daily report: say which visits (#195) (#196)",
        },
    )

    record = json.loads((state / "deploys.jsonl").read_text())
    assert record["prs"] == [207, 196]  # oldest first; the last (#N) is the pull request
    assert (record["from"], record["to"], record["result"]) == (current, target, "deployed")
