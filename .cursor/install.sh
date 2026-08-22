#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for TubeSync (YouTube PVR / yt-dlp / ffmpeg).
# Mirrors .github/workflows/ci.yaml "test" job so agents can run Django tests
# and yt-dlp/ffmpeg tooling used by MediaNest YouTube processing.
set -euo pipefail

cd "$(git rev-parse --show-toplevel 2>/dev/null || echo /workspace)"

export DEBIAN_FRONTEND=noninteractive
export PATH="/usr/local/bin:${HOME}/.local/bin:${PATH}"

sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  python3-dev python3-pip python3-venv python3-libsass \
  default-libmysqlclient-dev pkg-config gcc g++ make \
  libjpeg-dev libwebp-dev zlib1g-dev \
  ffmpeg unzip curl ca-certificates

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  sudo install -m 0755 "${HOME}/.local/bin/uv" /usr/local/bin/uv
  sudo install -m 0755 "${HOME}/.local/bin/uvx" /usr/local/bin/uvx
fi

if ! command -v deno >/dev/null 2>&1; then
  tmpdir="$(mktemp -d)"
  curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip" \
    -o "${tmpdir}/deno.zip"
  sudo unzip -o -q "${tmpdir}/deno.zip" -d /usr/local/bin
  sudo chmod a+rx /usr/local/bin/deno
  rm -rf "${tmpdir}"
fi

uv --no-config --no-managed-python --no-progress \
  tool run pipenv requirements --dev --no-lock > /tmp/Pipfile-requirements.txt
uv --no-config --no-managed-python --no-progress \
  pip compile --format requirements.txt --generate-hashes \
  --output-file /tmp/Pipfile-requirements-with-hashes.txt \
  /tmp/Pipfile-requirements.txt
sudo env PATH="${PATH}" uv --no-config --no-managed-python --no-progress \
  pip install --python /usr/bin/python3 --system --break-system-packages --strict \
  --requirements /tmp/Pipfile-requirements-with-hashes.txt

mkdir -p "${HOME}/.config/TubeSync/config" \
  "${HOME}/.config/TubeSync/downloads/audio" \
  "${HOME}/.config/TubeSync/downloads/video"
sudo ln -sfn "${HOME}/.config/TubeSync/config" /config
sudo ln -sfn "${HOME}/.config/TubeSync/downloads" /downloads

if [ ! -f tubesync/tubesync/local_settings.py ]; then
  cp -p tubesync/tubesync/local_settings.py.example tubesync/tubesync/local_settings.py
fi

sudo python3 - <<'PY'
import glob
import pathlib
import shutil

import yt_dlp

dest = pathlib.Path(yt_dlp.__file__).resolve().parent
for src in glob.glob("patches/yt_dlp/**/*", recursive=True):
    src_path = pathlib.Path(src)
    if not src_path.is_file():
        continue
    relative = src_path.relative_to("patches/yt_dlp")
    target = dest / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, target)
PY

cd tubesync
python3 -B manage.py collectstatic --no-input --link
