"""Enrolling the household's faces: ``python -m gatekeeper.face_admin <command>``.

With ``FACE_RECOGNITION`` on, Gatekeeper keeps the faces of people who went in by
the front door (most likely the household) for ``FACE_CANDIDATE_DAYS``. This groups
them, shows them, and enrolls the groups the owner names, with that person's
consent. Run it in the Gatekeeper container (``docker exec -it gatekeeper ...``).

  candidates [--all]   group the kept faces; a picture of each group in <FACE_DIR>/review
                       (lists the groups seen on 2+ visits; --all for every one)
  enroll NAME GROUP... add those groups' faces to NAME (needs --consent)
  list                 who is enrolled, and how many faces each
  forget NAME          delete NAME's faces
  purge                delete every enrolled face and every kept one

One person often comes out as several groups (daylight, night, the porch light):
enroll them all under the same name. More faces in more kinds of light recognize
them more often.
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np

from gatekeeper.faces import Gallery

LINK = 0.40  # faces this similar on average (cosine) are one group
SHEET_COLUMNS, SHEET_ROWS = 6, 3


def load_candidates(directory: Path) -> list[dict]:
    """Every kept face: its file, index in it, embedding, picture, time, and the name
    it matched when seen (if any)."""
    out = []
    for path in sorted(directory.glob("*.npz")):
        with np.load(path) as d:
            for i in range(len(d["embeddings"])):
                out.append(
                    {
                        "file": path.name,
                        "index": i,
                        "embedding": d["embeddings"][i].astype(np.float64),
                        "face": d["faces"][i],
                        "at": float(d["at"][i]),
                        "matched": str(d["names"][i]),
                    }
                )
    return out


def group(embeddings: np.ndarray, link: float = LINK) -> list[list[int]]:
    """Faces grouped by average-linkage clustering on cosine similarity, biggest first.
    The group-to-group similarities are updated in place (Lance-Williams), so two weeks
    of kept faces group in a moment."""
    n = len(embeddings)
    if not n:
        return []
    sim = (embeddings @ embeddings.T).astype(np.float64)
    np.fill_diagonal(sim, -np.inf)
    size = np.ones(n)
    members: list[list[int]] = [[i] for i in range(n)]
    while True:
        a, b = divmod(int(np.argmax(sim)), n)
        if sim[a, b] <= link:
            break
        merged = (sim[a] * size[a] + sim[b] * size[b]) / (size[a] + size[b])
        sim[a], sim[:, a] = merged, merged
        sim[a, a] = -np.inf
        sim[b], sim[:, b] = -np.inf, -np.inf
        size[a] += size[b]
        members[a] += members[b]
        members[b] = []
    return sorted((sorted(m) for m in members if m), key=len, reverse=True)


def jpeg_bytes(bgr: np.ndarray) -> bytes:
    """A JPEG of a BGR image (PyAV encodes it)."""
    import av

    frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(bgr), format="bgr24")
    codec = av.CodecContext.create("mjpeg", "w")
    codec.width, codec.height = frame.width, frame.height
    codec.pix_fmt, codec.time_base = "yuvj420p", Fraction(1, 25)
    packets = codec.encode(frame.reformat(format="yuvj420p")) + codec.encode(None)
    return b"".join(bytes(p) for p in packets)


def write_jpeg(path: Path, bgr: np.ndarray) -> None:
    path.write_bytes(jpeg_bytes(bgr))
    path.chmod(0o600)


def sheet(faces: list[np.ndarray]) -> np.ndarray:
    """Up to SHEET_COLUMNS x SHEET_ROWS faces in a grid."""
    n = SHEET_COLUMNS * SHEET_ROWS
    tiles = list(faces[:n]) + [np.zeros_like(faces[0])] * (n - min(n, len(faces)))
    rows = [
        np.hstack(tiles[r * SHEET_COLUMNS : (r + 1) * SHEET_COLUMNS]) for r in range(SHEET_ROWS)
    ]
    return np.vstack(rows)


def candidates(face_dir: Path) -> list[dict]:
    """Group the kept faces and write a picture of each group; the groups are saved
    for ``enroll``. Returns them (for printing)."""
    faces = load_candidates(face_dir / "candidates")
    review = face_dir / "review"
    shutil.rmtree(review, ignore_errors=True)
    review.mkdir(parents=True)
    review.chmod(0o700)
    groups = group(np.array([f["embedding"] for f in faces])) if faces else []
    summary = []
    for n, members in enumerate(groups, 1):
        chosen = [faces[i] for i in members]
        days = sorted({_day(f["at"]) for f in chosen})
        matched = sorted({f["matched"] for f in chosen if f["matched"]})
        write_jpeg(review / f"group-{n}.jpg", sheet([f["face"] for f in chosen]))
        summary.append(
            {
                "group": n,
                "faces": [[f["file"], f["index"]] for f in chosen],
                "visits": len({f["file"] for f in chosen}),
                "days": days,
                "matched": matched,
            }
        )
    (review / "groups.json").write_text(json.dumps(summary), encoding="utf-8")
    return summary


def enroll(face_dir: Path, name: str, numbers: list[int]) -> int:
    """Add the faces of groups ``numbers`` (from the last ``candidates``) to ``name``,
    and drop them from the kept faces. Returns how many."""
    groups = {g["group"]: g for g in _groups(face_dir)}
    missing = [n for n in numbers if n not in groups]
    if missing:
        raise SystemExit(f"no group {', '.join(map(str, missing))}: run 'candidates' first")
    chosen = {(f, i) for n in numbers for f, i in groups[n]["faces"]}
    faces = [
        f for f in load_candidates(face_dir / "candidates") if (f["file"], f["index"]) in chosen
    ]
    Gallery(face_dir / "gallery.json").enroll(name, [f["embedding"] for f in faces])
    drop(face_dir / "candidates", chosen)
    for n in numbers:
        (face_dir / "review" / f"group-{n}.jpg").unlink(missing_ok=True)
    return len(faces)


def drop(directory: Path, chosen: set[tuple[str, int]]) -> None:
    """Remove faces (file, index) from the kept ones; a file left empty is deleted."""
    for file in {f for f, _ in chosen}:
        path = directory / file
        with np.load(path) as d:
            data = {k: d[k] for k in d.files}
        keep = [i for i in range(len(data["embeddings"])) if (file, i) not in chosen]
        if not keep:
            path.unlink()
            continue
        for key in ("embeddings", "faces", "at", "names"):
            data[key] = data[key][keep]
        with path.open("wb") as f:
            np.savez_compressed(f, **data)


def _groups(face_dir: Path) -> list[dict]:
    path = face_dir / "review" / "groups.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _day(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%a %d %b")


def main(argv: list[str] | None = None) -> int:
    from gatekeeper.config import Settings

    parser = argparse.ArgumentParser(
        prog="python -m gatekeeper.face_admin",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("candidates")
    p.add_argument("--all", action="store_true", help="list groups seen on one visit too")
    p = sub.add_parser("enroll")
    p.add_argument("name")
    p.add_argument("groups", nargs="+", type=int)
    p.add_argument(
        "--consent",
        action="store_true",
        help="confirm that NAME agreed to be recognized by this doorbell",
    )
    sub.add_parser("list")
    p = sub.add_parser("forget")
    p.add_argument("name")
    sub.add_parser("purge")
    args = parser.parse_args(argv)
    face_dir = Settings().face_dir

    if args.command == "candidates":
        groups = candidates(face_dir)
        if not groups:
            print("No faces kept yet: they come from people who go in by the front door.")
            return 0
        shown = [g for g in groups if args.all or g["visits"] >= 2]
        print(f"{'group':>5}  {'faces':>5}  {'visits':>6}  days / already matched")
        for g in shown:
            matched = f"  (matched {', '.join(g['matched'])})" if g["matched"] else ""
            print(
                f"{g['group']:>5}  {len(g['faces']):>5}  {g['visits']:>6}  "
                f"{', '.join(g['days'])}{matched}"
            )
        if len(shown) < len(groups):
            print(f"({len(groups) - len(shown)} more seen on one visit only: --all)")
        print(f"\nPictures: {face_dir / 'review'}/group-<n>.jpg")
        print("Then: python -m gatekeeper.face_admin enroll NAME <group> ... --consent")
    elif args.command == "enroll":
        if not args.consent:
            print(
                f"Faces are biometric data. Enroll {args.name} only if they agreed to be "
                "recognized by this doorbell, then add --consent.",
                file=sys.stderr,
            )
            return 2
        n = enroll(face_dir, args.name, args.groups)
        print(f"Enrolled {n} face(s) for {args.name}.")
    elif args.command == "list":
        gallery = Gallery(face_dir / "gallery.json")
        data = json.loads(gallery.path.read_text(encoding="utf-8")) if gallery.path.exists() else {}
        people = data.get("people", {})
        if not people:
            print("Nobody enrolled.")
        for name, p in sorted(people.items()):
            print(f"{name}: {len(p['embeddings'])} face(s), since {_day(p['added'])}")
    elif args.command == "forget":
        gone = Gallery(face_dir / "gallery.json").forget(args.name)
        print(f"Forgot {args.name}." if gone else f"{args.name} isn't enrolled.")
    elif args.command == "purge":
        Gallery(face_dir / "gallery.json").purge()
        shutil.rmtree(face_dir / "candidates", ignore_errors=True)
        shutil.rmtree(face_dir / "review", ignore_errors=True)
        print("Deleted every enrolled face and every kept one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
