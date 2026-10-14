#!/bin/bash
# Export a YOLOv9 model to ONNX for Frigate's GPU detector (docker-compose.gpu.yml),
# following Frigate's documented build (docs.frigate.video, "Object Detectors", YOLOv9).
#
#   scripts/gpu/export-yolov9.sh [SIZE] [IMG_SIZE]     # defaults: s 320
#
# SIZE is t, s, m, c or e; IMG_SIZE must match config.gpu.yml's model width/height. The file
# lands in ${DATA_DIR}/frigate/config/model_cache/yolov9-SIZE-IMG_SIZE.onnx, which Frigate
# sees as /config/model_cache/. Building takes a few minutes (it installs YOLOv9's
# requirements) and needs internet access.
#
# YOLOv9 (github.com/WongKinYiu/yolov9) is GPL-3.0: you export it yourself; Gatekeeper
# doesn't ship the weights.
set -euo pipefail
cd "$(dirname "$0")/../.."
SIZE="${1:-s}"
IMG_SIZE="${2:-320}"
[ -f .env ] && { set -a; . ./.env; set +a; }
OUT="${DATA_DIR:-./data}/frigate/config/model_cache"
mkdir -p "$OUT"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

docker build "$work" --build-arg MODEL_SIZE="$SIZE" --build-arg IMG_SIZE="$IMG_SIZE" \
  --output "$work" -f- <<'EOF'
FROM python:3.11 AS build
RUN apt-get update && apt-get install --no-install-recommends -y cmake libgl1
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /bin/
WORKDIR /yolov9
ADD https://github.com/WongKinYiu/yolov9.git .
RUN uv pip install --system -r requirements.txt
RUN uv pip install --system onnx==1.18.0 onnxruntime onnx-simplifier==0.4.* onnxscript
ARG MODEL_SIZE
ARG IMG_SIZE
ADD https://github.com/WongKinYiu/yolov9/releases/download/v0.1/yolov9-${MODEL_SIZE}-converted.pt yolov9-${MODEL_SIZE}.pt
RUN sed -i "s/ckpt = torch.load(attempt_download(w), map_location='cpu')/ckpt = torch.load(attempt_download(w), map_location='cpu', weights_only=False)/g" models/experimental.py
RUN python3 export.py --weights ./yolov9-${MODEL_SIZE}.pt --imgsz ${IMG_SIZE} --simplify --include onnx
FROM scratch
ARG MODEL_SIZE
ARG IMG_SIZE
COPY --from=build /yolov9/yolov9-${MODEL_SIZE}.onnx /yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx
EOF

cp "$work/yolov9-$SIZE-$IMG_SIZE.onnx" "$OUT/"
echo "Exported $OUT/yolov9-$SIZE-$IMG_SIZE.onnx ($(du -h "$OUT/yolov9-$SIZE-$IMG_SIZE.onnx" | cut -f1))"
