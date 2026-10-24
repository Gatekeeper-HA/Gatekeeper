import json
import logging

import numpy as np
import pytest

from conftest import FakeClock
from gatekeeper.packages import (
    CELL,
    Baselines,
    Package,
    PackageModel,
    PackageWatcher,
    Shot,
    alike,
    changed_share,
    compare,
    light,
    square_crop,
    zone_cells,
)

PORCH = [(0.48, 0.78), (1.0, 0.78), (1.0, 1.0), (0.48, 1.0)]  # the rig's porch zone
BOX = (448, 400, 512, 464)  # where the test package is put, in the zone


def doorstep(seed=0, brightness=1.0):
    """A 640x480 porch: a gradient with some texture, like concrete and siding."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:480, 0:640]
    gray = 60 + 0.15 * x + 0.1 * y + rng.normal(0, 3, (480, 640))
    rgb = np.repeat(gray[:, :, None], 3, axis=2) * brightness
    return np.clip(rgb, 0, 255).astype(np.uint8)


def with_package(rgb, box=BOX):
    out = rgb.copy()
    x0, y0, x1, y1 = box
    out[y0:y1, x0:x1] = (150, 110, 70)  # cardboard
    return out


def zone():
    return zone_cells(PORCH, 480, 640)


def test_a_package_left_in_the_zone_is_one_patch_around_it():
    c = compare(doorstep(0), with_package(doorstep(1)), zone())
    assert c.judged
    assert c.patches == [BOX]
    assert changed_share(c.changed, BOX) == 1.0


def test_noise_and_changes_outside_the_zone_are_not_patches():
    after = doorstep(1)
    after[100:180, 100:180] = 255  # something on the siding, outside the zone
    c = compare(doorstep(0), after, zone())
    assert c.judged and c.patches == []


def test_a_patch_smaller_than_a_few_cells_is_noise():
    small = (448, 400, 448 + 2 * CELL, 400 + CELL)  # 2 cells: a leaf
    assert compare(doorstep(0), with_package(doorstep(1), small), zone()).patches == []


def busy_scene():
    """The zone as before, everything above it different (lights across the yard)."""
    after = doorstep(1)
    after[:360] = np.random.default_rng(9).integers(0, 255, (360, 640, 3), dtype=np.uint8)
    return after


@pytest.mark.parametrize(
    ("after", "reason"),
    [
        (doorstep(1, brightness=1.6), "exposure"),  # the camera's night mode, a light
        (busy_scene(), "scene"),
    ],
)
def test_frames_whose_lighting_changed_are_not_judged(after, reason):
    c = compare(doorstep(0), after, zone())
    assert not c.judged and reason in c.reason


def test_a_brightness_change_the_exposure_evens_out_is_judged():
    c = compare(doorstep(0), with_package(doorstep(1, brightness=1.2)), zone())
    assert c.judged and c.patches == [BOX]


def test_square_crop_stays_in_the_frame():
    rgb = doorstep()
    assert square_crop(rgb, (620, 460, 640, 480)).shape == (96, 96, 3)
    assert square_crop(rgb, (0, 0, 600, 400)).shape == (480, 480, 3)


def test_model_score_is_the_share_of_package_descriptions():
    model = PackageModel()
    n_package = len(model.PACKAGE)
    n = n_package + len(model.OTHER)

    class Vision:
        def run(self, names, feeds):
            assert feeds["pixel_values"].shape == (1, 3, 224, 224)
            return [np.eye(n, dtype=np.float32)[:1]]  # just like the first description

    model._resize = lambda crop: np.zeros((224, 224, 3), np.uint8)  # PyAV's job
    model._vision = Vision()
    model._text = np.eye(n, dtype=np.float32)
    assert model.score(doorstep(), BOX) == pytest.approx(1.0)
    model._text = np.roll(np.eye(n, dtype=np.float32), n_package, axis=0)
    assert model.score(doorstep(), BOX) < 0.01


class Porch:
    """The watcher's world: who's there, the frames it gets, the model's scores."""

    def __init__(self, tmp_path):
        self.clock = FakeClock(1000.0)
        self.someone = False
        self.scene = Shot(doorstep(0), b"empty")
        self.changes = []
        self.zone = PORCH
        self.lit = True
        self.watcher = PackageWatcher(
            present=lambda: self.someone,
            frame=lambda: self.scene,
            zone=lambda: self.zone,
            score=self.score,
            on_change=lambda kind, package, after, came: self.changes.append(
                (kind, package.box, after.jpeg, came)
            ),
            state_file=tmp_path / "packages.json",
            clock=self.clock,
        )

    def score(self, rgb, box):
        x0, y0, x1, y1 = box
        cardboard = (rgb[y0:y1, x0:x1] == (150, 110, 70)).all(axis=2).mean()
        return 0.9 if cardboard > 0.5 else 0.1

    def run(self, seconds):
        for _ in range(int(seconds / 5)):
            self.watcher.step()
            self.clock.now += 5

    def visit(self, leaves, seconds=30):
        self.someone = True
        self.run(seconds)
        self.someone = False
        self.scene = leaves
        self.run(20)


def test_a_package_left_and_later_picked_up(tmp_path):
    porch = Porch(tmp_path)
    porch.run(10)  # the empty doorstep
    porch.visit(Shot(with_package(doorstep(1)), b"with package"))
    ((kind, box, jpeg, came),) = porch.changes
    assert (kind, box, jpeg) == ("delivered", BOX, b"with package")
    assert came == 1010.0  # when the delivery person came
    assert json.loads((tmp_path / "packages.json").read_text())[0]["box"] == list(BOX)

    porch.visit(Shot(doorstep(2), b"empty again"))  # someone takes it
    assert [c[0] for c in porch.changes] == ["delivered", "picked_up"]
    assert porch.watcher.packages == []


def test_a_visit_that_leaves_nothing_changes_nothing(tmp_path):
    porch = Porch(tmp_path)
    porch.run(10)
    porch.visit(Shot(doorstep(1), b"x"))
    assert porch.changes == []


def test_a_change_that_is_not_a_package_is_not_alerted(tmp_path):
    porch = Porch(tmp_path)
    porch.score = lambda rgb, box: 0.2  # a doormat moved
    porch.watcher._score = porch.score
    porch.run(10)
    porch.visit(Shot(with_package(doorstep(1)), b"x"))
    assert porch.changes == []


def test_lighting_that_changed_during_the_visit_is_compared_again_later(tmp_path):
    porch = Porch(tmp_path)
    porch.run(10)
    # The porch light is still on when they've gone; it goes off two minutes later.
    porch.visit(Shot(with_package(doorstep(1, brightness=1.8)), b"lit"))
    assert porch.changes == []
    porch.scene = Shot(with_package(doorstep(2)), b"dark again")
    porch.run(120)
    assert [(c[0], c[2]) for c in porch.changes] == [("delivered", b"dark again")]


def test_packages_are_remembered_across_restarts(tmp_path):
    (tmp_path / "packages.json").write_text(
        json.dumps([{"box": list(BOX), "since": 5.0, "score": 0.8}])
    )
    porch = Porch(tmp_path)
    assert porch.watcher.packages == [Package(BOX, 5.0, 0.8)]
    porch.scene = Shot(with_package(doorstep(0)), b"still there")
    porch.run(10)
    porch.visit(Shot(doorstep(1), b"taken"))
    assert [c[0] for c in porch.changes] == ["picked_up"]


def test_nothing_is_judged_until_the_zone_is_known(tmp_path):
    porch = Porch(tmp_path)
    porch.zone = None
    porch.run(10)
    porch.visit(Shot(with_package(doorstep(1)), b"x"))
    assert porch.changes == []


def test_a_visit_whose_lighting_never_settles_counts_once_in_the_report(tmp_path, caplog):
    from gatekeeper.report import package_info

    caplog.set_level(logging.INFO, logger="gatekeeper.packages")
    porch = Porch(tmp_path)
    porch.run(10)
    # Night mode came on during the visit, and stays on for good.
    porch.visit(Shot(doorstep(1, brightness=1.8), b"night"))
    porch.run(700)
    lines = [r.getMessage() for r in caplog.records]
    assert package_info([], lines)["not_checked"] == {}  # still waiting for its light
    porch.run(12 * 3600)
    lines = [r.getMessage() for r in caplog.records]
    info = package_info([], lines)
    assert info["not_checked"] == {"lighting": 1}  # one visit, not one per retry
    assert info["retries"] >= 10  # every minute for 10 minutes, then waiting
    assert info["checked"] == 0


# When the light changed during the visit (Baselines).


def test_a_light_seen_before_is_compared_with_the_last_frame_in_it(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="gatekeeper.packages")
    porch = Porch(tmp_path)
    porch.scene = Shot(doorstep(0, brightness=1.6), b"lit, empty")  # a light on, earlier
    porch.run(70)
    porch.scene = Shot(doorstep(0), b"dark, empty")
    porch.run(70)
    # A delivery switches the light on, and it stays on.
    porch.visit(Shot(with_package(doorstep(1, brightness=1.6)), b"lit, with package"))
    assert [(c[0], c[1], c[2]) for c in porch.changes] == [("delivered", BOX, b"lit, with package")]
    assert "in the same light" in caplog.text
    assert "not checked yet" not in caplog.text  # no need to wait


def test_a_light_never_seen_waits_for_the_light_from_before_the_visit(tmp_path):
    porch = Porch(tmp_path)
    porch.run(10)
    came = porch.clock.now
    porch.visit(Shot(with_package(doorstep(1, brightness=1.6)), b"lit"))
    porch.run(3600)  # the light stays on for an hour: not compared yet
    assert porch.changes == []
    porch.scene = Shot(with_package(doorstep(2)), b"dark again")
    porch.run(120)
    ((kind, box, jpeg, since),) = porch.changes
    assert (kind, box, jpeg, since) == ("delivered", BOX, b"dark again", came)


def test_the_kind_of_light_is_the_cameras_mode_and_the_brightness():
    mono, colour = doorstep(0), doorstep(0).copy()
    colour[..., 0] = np.clip(colour[..., 0].astype(int) + 40, 0, 255)  # warmer: a lamp
    assert light(mono)[0] == "mono" and light(colour)[0] == "colour"
    assert alike(light(mono), light(doorstep(1, brightness=1.2)))
    assert not alike(light(mono), light(doorstep(1, brightness=1.8)))
    assert not alike(light(mono), light(colour))


def test_frames_in_each_light_are_kept_across_restarts(tmp_path):
    clock = FakeClock(1_000_000.0)
    frames = {b"dark": doorstep(0), b"lit": doorstep(0, brightness=1.6)}
    keep = Baselines(tmp_path / "b", clock=clock, decode=frames.get)
    keep.add(Shot(frames[b"dark"], b"dark"), clock.now - 60)
    keep.add(Shot(frames[b"lit"], b"lit"), clock.now - 3 * 86400)  # too old: dropped
    keep.add(Shot(frames[b"lit"], b"lit"), clock.now - 30)
    again = Baselines(tmp_path / "b", clock=clock, decode=frames.get)
    found = again.like(Shot(doorstep(1, brightness=1.6), b"x"), clock.now)
    assert [(round(at), s.jpeg) for at, s in found] == [(round(clock.now - 30), b"lit")]
    assert len(list((tmp_path / "b").glob("*.jpg"))) == 2

    clock.now += 3 * 86400
    assert Baselines(tmp_path / "b", clock=clock, decode=frames.get).before(clock.now) == []


def test_frigates_frame_keeps_its_shape(tmp_path):
    pytest.importorskip("av")
    from gatekeeper.face_admin import write_jpeg
    from gatekeeper.packages import decode_jpeg

    path = tmp_path / "wide.jpg"
    write_jpeg(path, np.zeros((720, 1280, 3), np.uint8))  # a 16:9 camera
    assert decode_jpeg(path.read_bytes()).shape == (480, 854, 3)
