import pytest

from gatekeeper.camera import photo_sharpness, sharpness


def test_a_sharp_page_scores_higher_than_a_blurred_one():
    np = pytest.importorskip("numpy")

    rng = np.random.default_rng(0)
    page = np.full((480, 640), 200.0)
    page[96:384, 128:512] = rng.choice([30.0, 220.0], size=(288, 384))  # print, in the middle
    k = np.ones(9) / 9  # a moving hand: blur along the rows
    blurred = np.apply_along_axis(lambda r: np.convolve(r, k, mode="same"), 1, page)
    assert sharpness(page) > 5 * sharpness(blurred)
    # Only the middle counts: sharp stripes at the edges (the siding) don't.
    edges = blurred.copy()
    edges[:, :64] = rng.choice([0.0, 255.0], size=(480, 64))
    assert sharpness(edges) == pytest.approx(sharpness(blurred))


def test_a_photo_that_cannot_be_read_has_no_score(tmp_path):
    broken = tmp_path / "x.jpg"
    broken.write_bytes(b"not a jpeg")
    assert photo_sharpness(broken) is None
