import json

import numpy as np
import pytest

from gatekeeper import face_admin
from gatekeeper.faces import Gallery


def unit(*values):
    v = np.array(values + (0.0,) * (128 - len(values)))
    return v / np.linalg.norm(v)


def kept(face_dir, event_id, embeddings, at=1_792_000_000.0):
    """A candidate file, as FaceWatcher.keep writes it."""
    d = face_dir / "candidates"
    d.mkdir(parents=True, exist_ok=True)
    n = len(embeddings)
    with (d / f"{event_id}.npz").open("wb") as f:
        np.savez_compressed(
            f,
            embeddings=np.array(embeddings, dtype=np.float32),
            faces=np.full((n, 112, 112, 3), 128, np.uint8),
            at=np.full(n, at),
            outcome=np.array("entered"),
            names=np.array([""] * n),
        )


@pytest.fixture
def face_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FACE_DIR", str(tmp_path / "faces"))
    # Writing the pictures needs PyAV, which CI doesn't install.
    monkeypatch.setattr(face_admin, "write_jpeg", lambda path, image: path.write_bytes(b"jpg"))
    return tmp_path / "faces"


def test_kept_faces_are_grouped_by_person(face_dir):
    kept(face_dir, "e1", [unit(1, 0.1), unit(1, 0.2), unit(0, 0, 1)])
    kept(face_dir, "e2", [unit(1, 0.15)])
    groups = face_admin.candidates(face_dir)
    assert [(len(g["faces"]), g["visits"]) for g in groups] == [(3, 2), (1, 1)]
    assert (face_dir / "review" / "group-1.jpg").exists()
    assert json.loads((face_dir / "review" / "groups.json").read_text())[0]["group"] == 1


def test_enrolling_a_group_needs_consent_and_moves_its_faces_to_the_gallery(face_dir, capsys):
    kept(face_dir, "e1", [unit(1, 0.1), unit(0, 0, 1)])
    kept(face_dir, "e2", [unit(1, 0.15)])
    assert face_admin.main(["candidates"]) == 0
    assert face_admin.main(["enroll", "Alex", "1"]) == 2
    assert "consent" in capsys.readouterr().err
    assert not (face_dir / "gallery.json").exists()

    assert face_admin.main(["enroll", "Alex", "1", "--consent"]) == 0
    assert "Enrolled 2 face(s) for Alex." in capsys.readouterr().out
    assert Gallery(face_dir / "gallery.json").match(unit(1, 0.12))[0] == "Alex"
    # The enrolled faces aren't kept any more; the other person's are.
    assert not (face_dir / "candidates" / "e2.npz").exists()
    with np.load(face_dir / "candidates" / "e1.npz") as d:
        assert len(d["embeddings"]) == 1

    assert face_admin.main(["list"]) == 0
    assert "Alex: 2 face(s)" in capsys.readouterr().out


def test_enrolling_an_unknown_group_fails(face_dir):
    with pytest.raises(SystemExit, match="no group 3"):
        face_admin.enroll(face_dir, "Alex", [3])


def test_forget_and_purge_delete_faces_for_good(face_dir, capsys):
    kept(face_dir, "e1", [unit(1)])
    Gallery(face_dir / "gallery.json").enroll("Alex", [unit(1)])
    assert face_admin.main(["forget", "Alex"]) == 0
    assert Gallery(face_dir / "gallery.json").names == []
    face_admin.main(["candidates"])
    assert face_admin.main(["purge"]) == 0
    assert not (face_dir / "candidates").exists() and not (face_dir / "review").exists()
    assert "Deleted every enrolled face" in capsys.readouterr().out


def test_sheets_hold_a_grid_of_faces():
    faces = [np.full((112, 112, 3), k, np.uint8) for k in range(4)]
    grid = face_admin.sheet(faces)
    rows, cols = face_admin.SHEET_ROWS, face_admin.SHEET_COLUMNS
    assert grid.shape == (112 * rows, 112 * cols, 3)
    assert grid[0, 112 * 3, 0] == 3 and grid[-1, -1, 0] == 0


def test_candidates_lists_people_seen_on_several_visits(face_dir, capsys):
    kept(face_dir, "e1", [unit(1, 0.1), unit(0, 0, 1)])
    kept(face_dir, "e2", [unit(1, 0.15)])
    face_admin.main(["candidates"])
    out = capsys.readouterr().out
    assert "    1      2       2" in out and "1 more seen on one visit only" in out
    face_admin.main(["candidates", "--all"])
    assert "    2      1       1" in capsys.readouterr().out
