import contextlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("custom_updater", Path(__file__).with_name("custom_updater.py"))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def fake_app(path, value="old", version="4.0.20"):
    contents = path / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": updater.APP_ID,
                      "CFBundleShortVersionString": version}, stream)
    (contents / "binary").write_text(value)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "CC Switch.app"
        self.candidate = self.root / "candidate.app"
        self.state = self.root / "state"
        self.state.mkdir()
        fake_app(self.app)
        fake_app(self.candidate, "new", "4.0.21")

    def copy_command(self, args, **kwargs):
        if args[0] == "/usr/bin/ditto":
            updater.shutil.copytree(args[1], args[2], dirs_exist_ok=True)
        return b""

    def test_install_preserves_complete_backup(self):
        with patch.object(updater, "command", side_effect=self.copy_command):
            backup = updater.install_candidate(self.candidate, self.app, self.state, None)
        self.assertEqual((self.app / "Contents/binary").read_text(), "new")
        self.assertEqual((backup / "Contents/binary").read_text(), "old")
        self.assertFalse(list(self.root.glob(".CC Switch.*.app")))

    def test_failed_swap_restores_previous_app(self):
        replace = updater.os.replace
        calls = 0

        def fail_new_bundle(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated install failure")
            return replace(source, destination)

        with patch.object(updater, "command", side_effect=self.copy_command), \
                patch.object(updater.os, "replace", side_effect=fail_new_bundle):
            with self.assertRaises(OSError):
                updater.install_candidate(self.candidate, self.app, self.state, None)
        self.assertEqual((self.app / "Contents/binary").read_text(), "old")
        self.assertFalse(list(self.root.glob(".CC Switch.*.app")))

    def test_failed_verification_never_moves_current_app(self):
        def fail_verify(args, **kwargs):
            if args[0].endswith("codesign"):
                raise updater.UpdateError("invalid bundle")
            return self.copy_command(args)

        with patch.object(updater, "command", side_effect=fail_verify):
            with self.assertRaises(updater.UpdateError):
                updater.install_candidate(self.candidate, self.app, self.state, None)
        self.assertEqual((self.app / "Contents/binary").read_text(), "old")

    def test_same_or_older_release_does_not_run_build_or_install(self):
        for tag in ["v4.0.20", "v4.0.19"]:
            with contextlib.redirect_stdout(io.StringIO()), patch.object(updater, "command") as run:
                self.assertFalse(updater.update_locked(self.root, "a" * 40, "refs/heads/custom",
                                                       self.state, self.app, False, False, tag))
                run.assert_not_called()

    def test_conflict_stops_before_build_and_install(self):
        def conflict(args, **kwargs):
            if args[1:3] == ["rev-parse", "--verify"]:
                return ("b" * 40).encode()
            if args[1:3] == ["rev-parse", "HEAD"]:
                return ("a" * 40).encode()
            if args[1] == "rev-parse":
                return ("c" * 40).encode()
            if args[1] == "diff":
                return b"custom patch"
            if args[1] == "show":
                return b"icon"
            if args[1] == "apply":
                raise updater.UpdateError("patch conflict")
            return b""

        with contextlib.redirect_stdout(io.StringIO()), \
                patch.object(updater, "command", side_effect=conflict) as run, \
                patch.object(updater.subprocess, "run"), \
                patch.object(updater, "install_candidate") as install:
            with self.assertRaisesRegex(updater.UpdateError, "conflict"):
                updater.update_locked(self.root, "a" * 40, "refs/heads/custom",
                                      self.state, self.app, False, False, "v4.0.21")
            install.assert_not_called()
            self.assertFalse(any(call.args[0][0] == "pnpm" for call in run.call_args_list))
        self.assertEqual((self.app / "Contents/binary").read_text(), "old")

    def test_concurrent_update_is_rejected(self):
        config = self.root / "custom-updater.json"
        config.write_text(json.dumps({"source_repo": str(self.root), "base_ref": "a" * 40,
                                      "custom_ref": "refs/heads/custom"}))
        state = config.parent / "custom-updater"
        state.mkdir(exist_ok=True)
        with (state / "update.lock").open("a") as lock:
            updater.fcntl.flock(lock, updater.fcntl.LOCK_EX | updater.fcntl.LOCK_NB)
            with self.assertRaisesRegex(updater.UpdateError, "already running"):
                updater.update(config, self.app)

    def test_release_tag_is_not_a_command_or_path(self):
        for value in ["../../main", "--upload-pack=bad", "v4.1.0-beta", "4.1", "v4.1.0;touch x"]:
            with self.assertRaises(updater.UpdateError):
                updater.version(value)
        self.assertGreater(updater.version("v4.0.20"), updater.version("4.0.9"))

    def test_real_three_way_patch_preserves_new_upstream_changes(self):
        repo = self.root / "source"
        repo.mkdir()

        def git(*args):
            return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *args],
                                           cwd=repo, stderr=subprocess.DEVNULL)

        git("init", "-q")
        git("config", "user.name", "Updater Test")
        git("config", "user.email", "test@example.invalid")
        git("config", "commit.gpgsign", "false")
        (repo / "package.json").write_text('{"version":"4.0.20"}')
        (repo / "README.md").write_text("old upstream\n")
        git("add", ".")
        git("commit", "-qm", "upstream base")
        base = git("rev-parse", "HEAD").decode().strip()
        git("checkout", "-qb", "custom")
        files = {
            "src-tauri/src/tray.rs": "const TRAY_TITLE_FONT_SIZE: f64 = 13.0;\n"
                "const TRAY_TITLE_HORIZONTAL_PADDING: f64 = 4.0;\nfn style_tray_quota_title() {}\n",
            "src-tauri/src/commands/settings.rs": "custom_updater::install(app)\n",
            "src-tauri/tauri.conf.json": json.dumps({"bundle": {"resources": {"helper.py": "custom-updater.py"}}}),
        }
        for name, text in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        icon = repo / "src-tauri/icons/tray/macos/statusbar_template_3x.png"
        icon.parent.mkdir(parents=True)
        icon.write_bytes(b"custom icon")
        git("add", ".")
        git("commit", "-qm", "customization")
        custom_patch = git("diff", "--binary", base, "HEAD")
        git("checkout", "--detach", base)
        (repo / "package.json").write_text('{"version":"4.0.21"}')
        (repo / "README.md").write_text("new upstream feature\n")
        git("commit", "-qam", "new official release")
        updater.command(["git", "apply", "--3way", "--index", "-"], cwd=repo, data=custom_patch)
        updater.verify_customization(repo, updater.hashlib.sha256(b"custom icon").hexdigest())
        self.assertEqual(json.loads((repo / "package.json").read_text())["version"], "4.0.21")
        self.assertEqual((repo / "README.md").read_text(), "new upstream feature\n")


if __name__ == "__main__":
    unittest.main()
