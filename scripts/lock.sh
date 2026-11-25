#!/bin/sh
# Regenerate the image's pinned dependencies, requirements.lock and requirements-cuda.lock,
# with uv. Only releases at least COOLDOWN_DAYS old (7 by default) are used: a broken or
# malicious release usually comes to light and is pulled within days, before it could reach
# a hub.
#
#   scripts/lock.sh            keep each package's pinned version where it still fits (after
#                              a change to pyproject.toml or requirements-cuda.in)
#   scripts/lock.sh --upgrade  move every package to its newest version past the cooldown
#
# The weekly workflow (.github/workflows/deps.yml) runs it with --upgrade. A security fix that
# can't wait goes in by hand: uv pip compile's --upgrade-package, without the cutoff.
#
# Needs uv (https://docs.astral.sh/uv/). Works with GNU or BSD date.
set -eu
cd "$(dirname "$0")/.."

days=${COOLDOWN_DAYS:-7}
python=3.13
cutoff=$(date -u -d "$days days ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null ||
  date -u -v-"$days"d +%Y-%m-%dT%H:%M:%SZ)
upgrade=""
if [ "${1:-}" = "--upgrade" ]; then
  upgrade="--upgrade"
elif [ -n "${1:-}" ]; then
  echo "usage: scripts/lock.sh [--upgrade]" >&2
  exit 2
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# The image's packages, for linux x86_64 and aarch64 (pyproject.toml, [tool.uv]
# environments). torch alone comes from PyTorch's CPU-only index ([tool.uv.sources]). That
# index gives no publish times, so the cutoff can't apply to torch; everything else comes
# from PyPI and keeps it.
# shellcheck disable=SC2086
uv pip compile pyproject.toml --extra speech --extra talkback --extra doorbell --extra remote \
  --python-version "$python" --universal --no-header --annotation-style line \
  --emit-index-url --exclude-newer "$cutoff" --exclude-newer-package torch=false \
  $upgrade -o requirements.lock --quiet
# pip (the Dockerfile) finds torch's CPU build through PyTorch's page for torch alone, so
# nothing else comes from there. Every line's "sys_platform == 'linux'" says nothing in a
# Linux-only file.
{
  echo "# Pinned dependencies for the gatekeeper image (Python $python, linux x86_64 + aarch64)."
  echo "# Made by scripts/lock.sh: releases from before $cutoff ($days-day cooldown)."
  echo "# Don't edit by hand; change pyproject.toml, then run scripts/lock.sh."
  sed -e "s/ ; sys_platform == 'linux'\( \|$\)/\1/" \
    -e 's|^--index-url https://pypi.org/simple$|&\n--find-links https://download.pytorch.org/whl/torch/|' \
    requirements.lock
} >"$tmp/lock" && mv "$tmp/lock" requirements.lock

# The CUDA libraries for the GPU image (Dockerfile VARIANT=cuda), linux x86_64, with
# requirements.lock as constraints (without its --index-url lines, which uv doesn't take
# there) so the packages both have match.
grep -v '^--' requirements.lock >"$tmp/constraints.txt"
# shellcheck disable=SC2086
uv pip compile requirements-cuda.in -c "$tmp/constraints.txt" \
  --python-version "$python" --python-platform x86_64-manylinux_2_28 \
  --no-header --annotation-style line --exclude-newer "$cutoff" \
  $upgrade -o requirements-cuda.lock --quiet
{
  echo "# CUDA libraries for the GPU image (Dockerfile VARIANT=cuda), Python $python, linux x86_64."
  echo "# Made by scripts/lock.sh: releases from before $cutoff ($days-day cooldown)."
  echo "# Don't edit by hand; change requirements-cuda.in, then run scripts/lock.sh."
  sed 's|-c [^ ]*constraints\.txt|-c requirements.lock|' requirements-cuda.lock
} >"$tmp/cuda" && mv "$tmp/cuda" requirements-cuda.lock

echo "requirements.lock and requirements-cuda.lock: releases from before $cutoff"
