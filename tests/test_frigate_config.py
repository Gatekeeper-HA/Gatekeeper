"""frigate/config/config.gpu.yml must be config.yml plus the GPU parts only, so zones,
masks and recording settings can't drift apart between the CPU and GPU setups."""

from pathlib import Path

import yaml

CONFIG = Path(__file__).parent.parent / "frigate" / "config"


def load(name):
    return yaml.safe_load((CONFIG / name).read_text(encoding="utf-8"))


def test_gpu_config_is_the_cpu_config_plus_the_gpu_detector():
    cpu, gpu = load("config.yml"), load("config.gpu.yml")

    assert gpu.pop("detectors") == {"onnx": {"type": "onnx"}}
    model = gpu.pop("model")
    assert model["model_type"] == "yolo-generic"
    assert model["path"].startswith("/config/model_cache/")
    for camera in gpu["cameras"].values():
        assert camera["ffmpeg"].pop("hwaccel_args") == "preset-nvidia"

    assert gpu == cpu


def test_the_model_size_matches_the_export_script_default():
    model = load("config.gpu.yml")["model"]
    assert (model["width"], model["height"]) == (320, 320)
    assert model["path"].endswith("yolov9-s-320.onnx")
    script = (CONFIG.parent.parent / "scripts" / "gpu" / "export-yolov9.sh").read_text()
    assert 'SIZE="${1:-s}"' in script and 'IMG_SIZE="${2:-320}"' in script
