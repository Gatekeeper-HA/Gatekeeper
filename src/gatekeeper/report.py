"""Daily report on a running Gatekeeper: what happened at the door in the last 24 hours,
checked against what Gatekeeper should do.

It runs inside the gatekeeper container (``python -m gatekeeper.report``), so the report
always comes from the code that's deployed. ``scripts/rig/daily-report.sh`` passes in what
only the host can see (restarts, recording audio, deploys, when each container was
created) as JSON in ``REPORT_HOST``,
and the containers' logs on stdin. The report is printed as JSON; ``--render text`` or
``--render markdown`` turns that JSON into the notification or the published page.

Keep it current: a change that adds a feature or fixes a bug also adds the check or the
number that shows it working on a live porch (CONTRIBUTING.md, "The daily report").
"""

from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import math
import os
import re
import sys
import time
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

SCHEMA = 2
HOURS = 24

# What the checks hold the live system to.
NOTIFY_SECONDS = 3.0  # from the visit's classification to the notification sent
DETECTOR_MS = 100.0  # Frigate's inference time
TRANSCRIBE_P90_SECONDS = {"cuda": 3.0, "cpu": 8.0}  # per answer, by Whisper's device
# From the end of the visitor's answer to the reply starting (the conversation engine's
# reply_latency): end-of-speech silence, Whisper, and queueing the reply.
REPLY_P90_SECONDS = {"cuda": 3.5, "cpu": 7.0}
MIN_CAMERA_FPS = 1.0
FRIGATE_ERRORS = 50  # one camera dropout logs about 20 error lines
MAX_SKIPPED_SHARE = 0.1  # of the camera's frames that detection drops

# Known, harmless log lines: counted, not listed.
NOISE = [
    re.compile(p)
    for p in (
        r"unauthenticated requests to the HF Hub",
        r"Defaulting repo_id to hexgrad/Kokoro-82M",
        r"NNPACK",
        r"dropout option adds dropout",
        r"weight_norm` is deprecated",
        r"torch\.jit\.script` is deprecated",
        r"Did not detect hwaccel",  # Frigate decodes video on the CPU (config.gpu.yml)
    )
]

# Gatekeeper's text log format (logs.TEXT_FORMAT).
GATEKEEPER_LINE = re.compile(
    r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (?P<level>[A-Z]+)\s+(?P<logger>\S+) "
    r"\[(?P<event>[^\]]*)\] (?P<message>.*)$"
)
FRIGATE_LINE = re.compile(
    r"^\S+ \S+\s+\[(?P<time>[^\]]+)\]\s+(?P<logger>\S+)\s+(?P<level>[A-Z]+)\s+: (?P<message>.*)$"
)
PYTHON_WARNING = re.compile(r"^(?P<file>/\S+\.py):\d+: (?P<category>\w*Warning): (?P<message>.*)$")
WHISPER_LOAD = re.compile(
    r"loading Whisper model: (?P<model>\S+) on (?P<device>\w+) \((?P<compute_type>\w+)\)"
)
WHISPER_FALLBACK = "Whisper couldn't run on the GPU"
TRANSCRIBED = re.compile(
    r"transcribed (?P<audio>[\d.]+) s of audio in (?P<seconds>[\d.]+) s"
    r"(?: \(features (?P<features>[\d.]+) s, model (?P<model>[\d.]+) s; load (?P<load>-?[\d.]+)\))?"
)
MADE_UP = "no speech where Whisper heard it"  # interaction: an answer Whisper made up
NOT_TRANSCRIBED = "no speech: not transcribed"  # interaction: a silent turn

LEVELS = {"WARNING": 1, "ERROR": 2, "CRITICAL": 3, "TRACEBACK": 3}
ERROR_LEVELS = {"ERROR", "CRITICAL", "TRACEBACK"}
MAX_LOG_KINDS = 20
# Visit outcomes that are the household, deliberately not greeted (the door zone).
NOT_GREETED = {"left", "returned"}


def local(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%a %H:%M")


def visit_start(visit: dict) -> float:
    """When the visit started, from its Frigate event id (or press-<ts>)."""
    try:
        return float(visit["event_id"].removeprefix("press-").split("-")[0])
    except (KeyError, ValueError):
        return visit.get("ts", 0.0)


def utc_timestamp(iso: str) -> float | None:
    """A UTC time from ``docker inspect`` (``2026-10-14T01:36``, maybe with seconds)."""
    try:
        return datetime.datetime.fromisoformat(iso[:19]).replace(tzinfo=datetime.UTC).timestamp()
    except (TypeError, ValueError):
        return None


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


# Logs ------------------------------------------------------------------------------


def split_sources(text: str) -> dict[str, list[str]]:
    """The logs as ``### <source>`` sections (docker logs of each container)."""
    sources: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("### "):
            current = sources.setdefault(line[4:].strip(), [])
        elif current is not None:
            current.append(line)
    return sources


def log_events(lines: list[str]) -> list[dict]:
    """Warnings, errors and tracebacks in a container's log. A traceback right after an
    error line (``log.exception``) belongs to it."""
    events: list[dict] = []
    last_line = -2
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Traceback (most recent call last):"):
            start = i
            i += 1
            frames = []
            while i < len(lines) and lines[i][:1] in (" ", "\t"):
                if lines[i].lstrip().startswith("File "):
                    frames.append(lines[i].strip())
                i += 1
            exception = lines[i].strip() if i < len(lines) else "?"
            i += 1
            where = frames[-1] if frames else ""
            if events and last_line == start - 1 and events[-1]["exception"] is None:
                events[-1]["exception"] = exception
                events[-1]["where"] = where
            else:
                events.append(_event("TRACEBACK", "python", exception, "", exception, where))
            continue
        if m := GATEKEEPER_LINE.match(line) or FRIGATE_LINE.match(line):
            level, logger, message, time_ = m["level"], m["logger"], m["message"], m["time"]
        elif m := PYTHON_WARNING.match(line):
            level, logger = "WARNING", "python"
            message, time_ = f"{m['category']}: {m['message']} ({m['file']})", ""
        else:
            i += 1
            continue
        if level in LEVELS:
            events.append(_event(level, logger, message, time_))
            last_line = i
        i += 1
    return events


def _event(level, logger, message, time_, exception=None, where="") -> dict:
    return {
        "level": level,
        "logger": logger,
        "message": message,
        "time": time_,
        "exception": exception,
        "where": where,
    }


def normalize(message: str) -> str:
    """A log message without the parts that change each time: addresses, numbers, ids."""
    message = re.sub(r"0x[0-9a-fA-F]+", "0x?", message)
    message = re.sub(r"\[[\d.]+-\w+\]", "[id]", message)
    return re.sub(r"\d+(\.\d+)?", "N", message)[:200]


def summarize_logs(text: str) -> dict:
    """Warnings and errors per container, grouped by kind, most severe and frequent first."""
    out: dict = {"sources": {}, "ignored": 0}
    for source, lines in split_sources(text).items():
        if source == "whisper":  # Whisper's lines from Gatekeeper's whole log, see build()
            continue
        groups: dict[tuple, dict] = {}
        for e in log_events(lines):
            if any(p.search(e["message"]) for p in NOISE):
                out["ignored"] += 1
                continue
            key = (
                e["level"],
                e["logger"],
                normalize(e["message"]),
                normalize(e["exception"] or ""),
            )
            if key in groups:
                groups[key]["count"] += 1
                groups[key]["last"] = e["time"] or groups[key]["last"]
            else:
                groups[key] = {
                    "level": e["level"],
                    "logger": e["logger"],
                    "message": e["message"][:300],
                    "exception": e["exception"],
                    "where": e["where"],
                    "count": 1,
                    "first": e["time"],
                    "last": e["time"],
                }
        kinds = sorted(groups.values(), key=lambda g: (-LEVELS[g["level"]], -g["count"]))
        out["sources"][source] = {
            "kinds": len(kinds),
            "errors": sum(g["count"] for g in kinds if g["level"] in ERROR_LEVELS),
            "warnings": sum(g["count"] for g in kinds if g["level"] == "WARNING"),
            "top": kinds[:MAX_LOG_KINDS],
        }
    return out


def whisper_info(lines: Iterable[str]) -> dict:
    """Whisper's model and device (from its last load), each transcription's time, and how
    many answers were ignored as made up from noise."""
    info: dict = {"model": None, "device": None, "compute_type": None, "fallback": False}
    turns = []
    split: dict[str, list[float]] = {"features": [], "model": [], "load": []}
    made_up = silent = 0
    for line in lines:
        if m := WHISPER_LOAD.search(line):
            info.update(m.groupdict(), fallback=False)
        elif WHISPER_FALLBACK in line:
            info["fallback"] = True
        elif m := TRANSCRIBED.search(line):
            turns.append((float(m["audio"]), float(m["seconds"])))
            if m["model"]:
                for key in split:
                    split[key].append(float(m[key]))
        elif MADE_UP in line:
            made_up += 1
        elif NOT_TRANSCRIBED in line:
            silent += 1
    info["made_up_ignored"] = made_up
    info["silent_turns_skipped"] = silent
    if info["fallback"]:
        info["device"] = "cpu"
    seconds = [s for _, s in turns]
    info["transcriptions"] = len(turns)
    if turns:
        info["median_seconds"] = round(percentile(seconds, 0.5), 2)
        info["p90_seconds"] = round(percentile(seconds, 0.9), 2)
        info["max_seconds"] = round(max(seconds), 2)
        info["median_audio_seconds"] = round(percentile([a for a, _ in turns], 0.5), 1)
    if split["model"]:
        # Where the time goes (Gatekeeper#16): reading the audio and its features, the
        # model itself, and how busy the host's CPUs were.
        info["median_features_seconds"] = round(percentile(split["features"], 0.5), 2)
        info["median_model_seconds"] = round(percentile(split["model"], 0.5), 2)
        info["median_load"] = round(percentile(split["load"], 0.5), 1)
    return info


def reply_info(visits: list[dict]) -> dict:
    """How listening ended, how soon replies started after the answer (the conversation
    engine's ``endpoint`` and ``reply_latency`` visit fields), and the tailored
    conversations held (``flow``: emergency, law_enforcement, civic)."""
    seconds = [v["reply_latency"] for v in visits if v.get("reply_latency") is not None]
    info: dict = {
        "count": len(seconds),
        "endpoints": dict(collections.Counter(v["endpoint"] for v in visits if v.get("endpoint"))),
        "conversations": dict(collections.Counter(v["flow"] for v in visits if v.get("flow"))),
    }
    if seconds:
        info["median_seconds"] = round(percentile(seconds, 0.5), 2)
        info["p90_seconds"] = round(percentile(seconds, 0.9), 2)
        info["max_seconds"] = round(max(seconds), 2)
    return info


def frigate_info(stats: dict | None, camera: str) -> dict | None:
    if not stats:
        return None
    cam = stats.get("cameras", {}).get(camera, {})
    return {
        "detectors": {
            name: d.get("inference_speed") for name, d in stats.get("detectors", {}).items()
        },
        **{k: cam.get(k) for k in ("camera_fps", "process_fps", "detection_fps", "skipped_fps")},
    }


# The report ------------------------------------------------------------------------


@dataclass
class Inputs:
    now: float
    visits: list[dict]  # visit records that ended in the window
    window_visits: list[dict]  # ... plus the hour before, for the cooldown windows
    notifications: list[dict]  # notification attempts, from 10 min before the window
    tracked_since: float  # the first notification ever logged
    arrivals: list[dict] | None  # Frigate's person events in the trigger zones; None: no API
    stats: dict | None  # Frigate's /api/stats
    log_text: str
    host: dict
    camera: str = "front_door"
    cooldown: float = 90.0
    whisper_device: str = "auto"  # the WHISPER_DEVICE setting
    version: str = "?"
    hours: int = HOURS


def build(i: Inputs) -> dict:
    since = i.now - i.hours * 3600
    host = i.host
    # The household coming out of the front door, or back in soon after, isn't
    # greeted or notified (the door zone): counted on their own.
    left = [v for v in i.visits if v.get("outcome") == "left"]
    returned = [v for v in i.visits if v.get("outcome") == "returned"]
    visits = [v for v in i.visits if v.get("outcome") not in NOT_GREETED]
    outcomes = collections.Counter(v.get("outcome") for v in visits)

    # Every visit notified, in time?
    ok = {n["event_id"]: n for n in i.notifications if n.get("ok")}
    tracked = [v for v in visits if v["ts"] >= i.tracked_since]
    unnotified = [v for v in tracked if v["event_id"] not in ok]
    slowest = max((n["seconds"] for n in ok.values()), default=0.0)

    # Every porch arrival greeted? A Frigate event is covered if it started during a visit
    # or its cooldown (merged into it).
    windows = [(visit_start(v) - 5, v["ts"] + i.cooldown) for v in i.window_visits]
    arrivals = i.arrivals or []
    missed = [e for e in arrivals if not any(a <= e["start_time"] <= b for a, b in windows)]

    logs = summarize_logs(i.log_text)
    sources = split_sources(i.log_text)
    whisper = whisper_info(sources.get("whisper", []) + sources.get("gatekeeper", []))
    frigate = frigate_info(i.stats, i.camera)
    deploys = host.get("deploys", [])
    # A recreated container (a deploy) starts a new log: what came before is gone.
    logs["since"] = {
        name: local(ts)
        for name, created in host.get("created", {}).items()
        if (ts := utc_timestamp(created)) and ts > since
    }
    hangs = outcomes.get("timeout", 0) + host.get("hang_exits", 0) + host.get("restarts", 0)

    checks: list[dict] = []

    def check(name: str, passed: bool, problem: str) -> None:
        checks.append({"name": name, "ok": bool(passed), "problem": None if passed else problem})

    if i.arrivals is None:
        check("frigate_api", False, "Frigate's API didn't answer")
    else:
        check("arrivals_greeted", not missed, f"{len(missed)} porch arrival(s) not greeted")
    check("visits_notified", not unnotified, f"{len(unnotified)} visit(s) not notified")
    check("notify_speed", slowest <= NOTIFY_SECONDS, f"slowest notification {slowest:.1f} s")
    check("no_hangs", not hangs, f"{hangs} hang/restart(s)")
    check("no_visit_errors", not outcomes.get("error"), f"{outcomes.get('error')} visit error(s)")
    silent = host.get("silent_audio_hours", [])
    check("recording_audio", not silent, f"no recording audio in {len(silent)} hour(s)")
    check(
        "healthy", host.get("health", "healthy") == "healthy", f"Gatekeeper is {host.get('health')}"
    )
    gk_errors = logs["sources"].get("gatekeeper", {}).get("errors", 0)
    check("gatekeeper_log_errors", not gk_errors, f"{gk_errors} error(s) in Gatekeeper's log")
    frigate_errors = logs["sources"].get("frigate", {}).get("errors", 0)
    check(
        "frigate_log_errors",
        frigate_errors <= FRIGATE_ERRORS,
        f"{frigate_errors} error(s) in Frigate's log",
    )
    if frigate:
        fps = frigate["camera_fps"] or 0.0
        skipped = frigate["skipped_fps"] or 0.0
        check("camera_frames", fps >= MIN_CAMERA_FPS, f"camera at {fps:.1f} fps")
        check(
            "detection_keeps_up",
            skipped <= MAX_SKIPPED_SHARE * max(fps, 1.0),
            f"detection skips {skipped:.1f} of {fps:.1f} fps",
        )
        slow = {k: v for k, v in frigate["detectors"].items() if (v or 0) > DETECTOR_MS}
        check(
            "detector_speed",
            not slow,
            ", ".join(f"{k} detector at {v:.0f} ms" for k, v in slow.items()),
        )
    if whisper["device"]:
        on_cpu_by_mistake = whisper["fallback"] or (
            i.whisper_device == "cuda" and whisper["device"] != "cuda"
        )
        check("whisper_device", not on_cpu_by_mistake, "Whisper fell back to the CPU")
    if whisper["transcriptions"]:
        limit = TRANSCRIBE_P90_SECONDS["cuda" if whisper["device"] == "cuda" else "cpu"]
        check(
            "transcription_speed",
            whisper["p90_seconds"] <= limit,
            f"Whisper p90 {whisper['p90_seconds']:.1f} s per answer",
        )
    replies = reply_info(visits)
    if replies["count"]:
        limit = REPLY_P90_SECONDS["cuda" if whisper["device"] == "cuda" else "cpu"]
        check(
            "reply_speed",
            replies["p90_seconds"] <= limit,
            f"replies p90 {replies['p90_seconds']:.1f} s after the answer",
        )
    if replies["endpoints"]:
        lost = replies["endpoints"].get("tap_lost", 0)
        check("audio_tap", not lost, f"the audio tap was lost during {lost} visit(s)")
    failed_deploys = [d for d in deploys if d.get("result") not in ("deployed", "moved")]
    check("deploys", not failed_deploys, f"{len(failed_deploys)} failed deploy(s)")

    return {
        "schema": SCHEMA,
        "generated": datetime.datetime.fromtimestamp(i.now).isoformat(timespec="seconds"),
        "hours": i.hours,
        "window": [local(since), local(i.now)],
        "gatekeeper": {
            "version": i.version,
            "commit": host.get("commit"),
            "subject": host.get("subject"),
            "image": host.get("image"),
            "health": host.get("health"),
            "up_since_utc": host.get("started"),
        },
        "checks": checks,
        "visits": [
            {"start": local(visit_start(v)), **{k: val for k, val in v.items() if k != "ts"}}
            for v in i.visits
        ],
        "counts": {
            "visits": len(visits),
            "left_by_door": len(left),
            "returned": len(returned),
            "triggers": dict(collections.Counter(v.get("trigger", "person") for v in visits)),
            "classifications": dict(
                collections.Counter(v.get("classification") or "pressed" for v in visits)
            ),
            "outcomes": dict(outcomes),
        },
        "arrivals": {
            "total": len(arrivals) if i.arrivals is not None else None,
            "missed": [local(e["start_time"]) for e in missed],
        },
        "notifications": {
            "tracked": len(tracked),
            "notified": len(tracked) - len(unnotified),
            "unnotified": [v["event_id"] for v in unnotified],
            "slowest_seconds": round(slowest, 2),
            "failed_attempts": sum(1 for n in i.notifications if not n.get("ok")),
        },
        "hangs": {
            "timeouts": outcomes.get("timeout", 0),
            "watchdog_exits": host.get("hang_exits", 0),
            "restarts": host.get("restarts", 0),
        },
        "recording_audio": {"silent_hours": silent},
        "whisper": whisper,
        "replies": replies,
        "frigate": frigate,
        "deploys": deploys,
        "logs": logs,
    }


# Rendering -------------------------------------------------------------------------


def problems(report: dict) -> list[str]:
    return [c["problem"] for c in report["checks"] if not c["ok"]]


def title(report: dict) -> str:
    found = problems(report)
    return "Gatekeeper daily report: " + ("; ".join(found) if found else "all clear")


def summary_lines(report: dict) -> list[str]:
    counts, n = report["counts"], report["notifications"]
    gk, w, f = report["gatekeeper"], report["whisper"], report["frigate"]
    labels = (
        ", ".join(
            f"{k} {v}" for k, v in sorted(counts["classifications"].items(), key=lambda kv: -kv[1])
        )
        or "none"
    )
    triggers = counts["triggers"]
    household = [
        f"{n} {what}"
        for n, what in (
            (counts.get("left_by_door"), "came out of the door and left"),
            (counts.get("returned"), "came back soon after"),
        )
        if n
    ]
    lines = [
        f"Last {report['hours']} h, to {report['window'][1]}",
        f"Visits: {counts['visits']} (person {triggers.get('person', 0)}, "
        f"button {triggers.get('button', 0)}): {labels}",
        f"Porch arrivals: {report['arrivals']['total']}, "
        f"not greeted {len(report['arrivals']['missed'])}"
        + (f"; {', '.join(household)} (not greeted, on purpose)" if household else ""),
        f"Notified: {n['notified']}/{n['tracked']}, slowest {n['slowest_seconds']:.2f} s",
    ]
    made_up = (
        f", {w['made_up_ignored']} made-up answer(s) ignored" if w.get("made_up_ignored") else ""
    ) + (
        f", {w['silent_turns_skipped']} silent turn(s) not transcribed"
        if w.get("silent_turns_skipped")
        else ""
    )
    if w["transcriptions"]:
        lines.append(
            f"Whisper: {w['model']} on {w['device']}, {w['transcriptions']} answers, "
            f"median {w['median_seconds']:.1f} s (p90 {w['p90_seconds']:.1f} s"
            + (
                f"; model {w['median_model_seconds']:.1f} s, features "
                f"{w['median_features_seconds']:.1f} s, CPU load {w['median_load']:.1f}"
                if "median_model_seconds" in w
                else ""
            )
            + f"){made_up}"
        )
    elif w["model"]:
        lines.append(f"Whisper: {w['model']} on {w['device']}, no answers{made_up}")
    r = report.get("replies") or {"count": 0, "endpoints": {}}
    if r.get("conversations"):
        lines.append(
            "Conversations: " + ", ".join(f"{k} {v}" for k, v in r["conversations"].items())
        )
    if r["count"]:
        lines.append(
            f"Replies: {r['count']}, median {r['median_seconds']:.1f} s after the answer "
            f"(p90 {r['p90_seconds']:.1f} s; the camera adds ~2.4 s)"
        )
    if r["endpoints"]:
        lines.append("Listening ended: " + ", ".join(f"{k} {v}" for k, v in r["endpoints"].items()))
    if f:
        detectors = ", ".join(f"{k} {v:.0f} ms" for k, v in f["detectors"].items() if v)
        lines.append(f"Frigate: {detectors or 'no detector'}, camera {f['camera_fps']} fps")
    for source, s in report["logs"]["sources"].items():
        if s["errors"]:
            first = next(g for g in s["top"] if g["level"] in ERROR_LEVELS)
            lines.append(
                f"{source} log: {s['errors']} error(s), e.g. "
                f"{(first['exception'] or first['message'])[:80]}"
            )
    if since := report["logs"].get("since"):
        lines.append(
            "Logs only since the container was recreated: "
            + ", ".join(f"{name} from {t}" for name, t in since.items())
            + " (errors and Whisper's times before then aren't counted)"
        )
    if report["deploys"]:
        lines.append(
            "Deploys: "
            + ", ".join(f"{d.get('to', '')[:7]} {d.get('result')}" for d in report["deploys"])
        )
    silent = report["recording_audio"]["silent_hours"]
    lines.append("Recording audio: " + ("OK" if not silent else "missing at " + ", ".join(silent)))
    lines.append(
        f"Gatekeeper {gk['version']} ({(gk['commit'] or '?')[:7]}), "
        f"{gk['health']}, up since {gk['up_since_utc']} UTC"
    )
    return lines


def render_text(report: dict) -> str:
    return "\n".join([title(report), *summary_lines(report)])


def _cell(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict) -> str:
    out = [
        f"# {title(report)}",
        "",
        *[f"- {line}" for line in summary_lines(report)],
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "|---|---|",
    ]
    out += [
        f"| {c['name']} | {'ok' if c['ok'] else '**' + _cell(c['problem']) + '**'} |"
        for c in report["checks"]
    ]
    out += ["", "## Visits", ""]
    if report["visits"]:
        out += [
            "| Start | Trigger | Outcome | Classification | Heard | Reply | Ended | Reply after |",
            "|---|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| {v['start']} | {_cell(v.get('trigger'))} | {_cell(v.get('outcome'))} | "
            f"{_cell(v.get('classification'))} | {_cell(v.get('transcript'))} | "
            f"{_cell(v.get('response'))} | {_cell(v.get('endpoint'))} | "
            f"{_cell(v.get('reply_latency'))} |"
            for v in report["visits"]
        ]
    else:
        out.append("None.")
    out += ["", "## Logs", ""]
    for source, s in report["logs"]["sources"].items():
        out.append(f"### {source}: {s['errors']} error(s), {s['warnings']} warning(s)")
        out.append("")
        for g in s["top"]:
            line = f"- **{g['level']}** ×{g['count']} `{g['logger']}`: {g['message']}"
            if g["exception"]:
                line += f" → `{g['exception']}` {g['where']}"
            out.append(line + f" ({g['first']} – {g['last']})")
        out.append("")
    out.append(f"Known harmless lines ignored: {report['logs']['ignored']}.")
    return "\n".join(out) + "\n"


# Collecting on the rig -------------------------------------------------------------


def read_log(path: Path, after: float) -> list[dict]:
    """Records from a JSONL log and its rotated files (events.jsonl, events-*.jsonl)."""
    rows = []
    for name in sorted(glob.glob(str(path.with_name(f"{path.stem}*{path.suffix}")))):
        with open(name, encoding="utf-8") as f:
            rows += [r for r in map(json.loads, f) if r.get("ts", 0) >= after]
    return rows


def fetch_json(url: str) -> dict | list | None:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.load(resp)
    except OSError:
        return None


def collect(
    settings,
    host: dict,
    log_text: str,
    now: float,
    hours: int = HOURS,
    fetch: Callable[[str], dict | list | None] = fetch_json,
) -> Inputs:
    from gatekeeper import __version__

    since = now - hours * 3600
    query = f"cameras={settings.camera_name}&labels=person&after={since:.0f}&limit=500"
    if settings.trigger_zone_list:
        query += "&zones=" + ",".join(settings.trigger_zone_list)
    return Inputs(
        now=now,
        visits=read_log(settings.event_log_file, since),
        window_visits=read_log(settings.event_log_file, since - 3600),
        notifications=read_log(settings.notification_log_file, since - 600),
        tracked_since=min(
            (n["ts"] for n in read_log(settings.notification_log_file, 0)), default=now
        ),
        arrivals=fetch(f"{settings.frigate_api}/api/events?{query}"),
        stats=fetch(f"{settings.frigate_api}/api/stats"),
        log_text=log_text,
        host=host,
        camera=settings.camera_name,
        cooldown=settings.cooldown_seconds,
        whisper_device=settings.whisper_device,
        version=__version__,
        hours=hours,
    )


def send(report: dict, settings) -> None:
    from gatekeeper.app import build_notify_backends
    from gatekeeper.notify import Notification

    found = problems(report)
    notification = Notification(
        title=title(report),
        message="\n".join(summary_lines(report)),
        tags=["warning"] if found else ["white_check_mark"],
        priority=4 if found else 2,
    )
    for backend in build_notify_backends(settings):
        backend.send(notification, None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gatekeeper.report",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--logs",
        help="the containers' logs ('-' for stdin), in sections that start with '### <container>'",
    )
    parser.add_argument("--hours", type=int, default=HOURS)
    parser.add_argument(
        "--notify", action="store_true", help="also send the summary via the configured notifiers"
    )
    parser.add_argument(
        "--render",
        choices=["text", "markdown"],
        help="read a report (JSON) on stdin and print it as text or Markdown",
    )
    args = parser.parse_args(argv)

    if args.render:
        report = json.load(sys.stdin)
        sys.stdout.write(
            (render_text(report) + "\n") if args.render == "text" else render_markdown(report)
        )
        return 0

    from gatekeeper.config import Settings

    settings = Settings()
    log_text = ""
    if args.logs == "-":
        log_text = sys.stdin.read()
    elif args.logs:
        log_text = Path(args.logs).read_text(encoding="utf-8", errors="replace")
    host = json.loads(os.environ.get("REPORT_HOST") or "{}")
    report = build(collect(settings, host, log_text, time.time(), args.hours))
    json.dump(report, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")
    if args.notify:
        send(report, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
