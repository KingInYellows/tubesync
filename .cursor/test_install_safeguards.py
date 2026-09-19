#!/usr/bin/env python3
"""Hermetic checks for Cloud Agent install.sh safeguards.

Does not install packages, touch host /config or /downloads, or talk to a database.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = REPO_ROOT / ".cursor" / "install.sh"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _env_with_sudo_stub(tmp: Path, extra: dict[str, str]) -> dict[str, str]:
    stub = tmp / "bin"
    stub.mkdir(exist_ok=True)
    sudo = stub / "sudo"
    sudo.write_text("#!/usr/bin/env bash\nexec \"$@\"\n", encoding="utf-8")
    sudo.chmod(sudo.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env["PATH"] = f"{stub}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}"
    env.update(extra)
    return env


class CloudAgentInstallSafeguardsTest(unittest.TestCase):
    def test_installer_uses_script_repo_when_invoked_from_another_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "medianest-checkout"
            other.mkdir()
            (other / ".git").mkdir()
            env = os.environ.copy()
            env["TUBESYNC_INSTALL_STOP_AFTER"] = "root"
            env["HOME"] = tmp
            result = subprocess.run(
                ["bash", str(INSTALL)],
                cwd=other,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(REPO_ROOT))

    def test_existing_correct_disposable_links_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            config_target = home / ".config" / "TubeSync" / "config"
            downloads_target = home / ".config" / "TubeSync" / "downloads"
            config_link = root / "mnt-config"
            downloads_link = root / "mnt-downloads"
            config_target.mkdir(parents=True)
            (downloads_target / "audio").mkdir(parents=True)
            (downloads_target / "video").mkdir(parents=True)
            config_link.symlink_to(config_target)
            downloads_link.symlink_to(downloads_target)
            env = _env_with_sudo_stub(
                root,
                {
                    "HOME": str(home),
                    "TUBESYNC_CONFIG_LINK": str(config_link),
                    "TUBESYNC_DOWNLOADS_LINK": str(downloads_link),
                    "TUBESYNC_INSTALL_STOP_AFTER": "links",
                },
            )
            result = subprocess.run(
                ["bash", str(INSTALL)],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config_link.resolve(), config_target.resolve())
            self.assertEqual(downloads_link.resolve(), downloads_target.resolve())

    def test_refuses_to_replace_unrelated_existing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            foreign = root / "other-config"
            foreign.mkdir()
            config_link = root / "mnt-config"
            downloads_link = root / "mnt-downloads"
            config_link.symlink_to(foreign)
            env = _env_with_sudo_stub(
                root,
                {
                    "HOME": str(home),
                    "TUBESYNC_CONFIG_LINK": str(config_link),
                    "TUBESYNC_DOWNLOADS_LINK": str(downloads_link),
                    "TUBESYNC_INSTALL_STOP_AFTER": "links",
                },
            )
            result = subprocess.run(
                ["bash", str(INSTALL)],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing to replace a non-disposable mount", result.stderr)
            self.assertTrue(config_link.is_symlink())
            self.assertEqual(config_link.resolve(), foreign.resolve())

    def test_generated_static_output_is_excluded_from_docker_context(self) -> None:
        rules = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        self.assertIn("tubesync/static", rules)


if __name__ == "__main__":
    unittest.main()
