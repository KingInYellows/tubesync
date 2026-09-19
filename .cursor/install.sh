#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for TubeSync (YouTube PVR / yt-dlp / ffmpeg).
# Mirrors .github/workflows/ci.yaml "test" job so agents can run Django tests
# and yt-dlp/ffmpeg tooling used by MediaNest YouTube processing.
set -euo pipefail

# Use this script's repository, not the caller's Git working directory.
# Invoking from MediaNest (or any other checkout) would otherwise install
# into the wrong tree and still rewrite host /config and /downloads.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
if [ ! -f tubesync/manage.py ] || [ ! -f Pipfile ]; then
  echo "ERROR: TubeSync install.sh must run against the TubeSync git root (cwd=${REPO_ROOT})" >&2
  exit 1
fi
if [ "${TUBESYNC_INSTALL_STOP_AFTER:-}" = "root" ]; then
  pwd
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
export PATH="/usr/local/bin:${HOME}/.local/bin:${PATH}"

if [ -z "${TUBESYNC_INSTALL_STOP_AFTER:-}" ]; then
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
fi

mkdir -p "${HOME}/.config/TubeSync/config" \
  "${HOME}/.config/TubeSync/downloads/audio" \
  "${HOME}/.config/TubeSync/downloads/video"

assert_disposable_symlink() {
  local linkpath="$1" intended="$2"
  local intended_resolved current
  intended_resolved="$(mkdir -p "$intended" && readlink -f "$intended")"
  if [ -L "$linkpath" ]; then
    current="$(readlink -f "$linkpath" || true)"
    if [ "$current" = "$intended_resolved" ]; then
      return 0
    fi
    echo "ERROR: ${linkpath} already points at ${current:-unresolved}; refusing to replace a non-disposable mount" >&2
    exit 1
  fi
  if [ -e "$linkpath" ]; then
    echo "ERROR: ${linkpath} exists and is not a TubeSync symlink; refusing to clobber" >&2
    exit 1
  fi
}

TUBESYNC_CONFIG_LINK="${TUBESYNC_CONFIG_LINK:-/config}"
TUBESYNC_DOWNLOADS_LINK="${TUBESYNC_DOWNLOADS_LINK:-/downloads}"
assert_disposable_symlink "$TUBESYNC_CONFIG_LINK" "${HOME}/.config/TubeSync/config"
assert_disposable_symlink "$TUBESYNC_DOWNLOADS_LINK" "${HOME}/.config/TubeSync/downloads"
sudo ln -sfn "${HOME}/.config/TubeSync/config" "$TUBESYNC_CONFIG_LINK"
sudo ln -sfn "${HOME}/.config/TubeSync/downloads" "$TUBESYNC_DOWNLOADS_LINK"
if [ "${TUBESYNC_INSTALL_STOP_AFTER:-}" = "links" ]; then
  exit 0
fi

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
