import contextlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import shutil
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

    def run_update_pipeline(self, *, prepare_only=False, fail_build=False,
                            fail_install=False, fail_git_cleanup=False):
        """Exercise update ordering and filesystem cleanup without downloading/building."""
        repo = self.root / "source"
        repo.mkdir()
        # A developer's unrelated build cache must survive the updater.
        cache = repo / "src-tauri/target/keep"
        cache.parent.mkdir(parents=True)
        cache.write_text("unrelated")
        workspaces = []

        def run(args, **kwargs):
            if args[0] == "git":
                if args[1:3] == ["rev-parse", "--verify"]:
                    return b"b" * 40
                if args[1:3] == ["rev-parse", "HEAD"]:
                    return b"a" * 40
                if args[1] == "rev-parse":
                    return b"c" * 40
                if args[1] == "diff":
                    return b"custom patch"
                if args[1] == "show":
                    return b"icon"
                if args[1:3] == ["worktree", "add"]:
                    workspace = Path(args[4])
                    workspace.mkdir()
                    (workspace / "package.json").write_text('{"version":"4.0.21"}')
                    workspaces.append(workspace)
                if args[1:3] == ["worktree", "remove"]:
                    if fail_git_cleanup:
                        raise updater.UpdateError("simulated unregister failure")
                    shutil.rmtree(args[4])
            elif args[:2] == ["pnpm", "install"]:
                (kwargs["cwd"] / "node_modules").mkdir()
            elif args[:3] == ["pnpm", "tauri", "build"]:
                workspace = kwargs["cwd"]
                target = Path(kwargs["env"]["CARGO_TARGET_DIR"])
                self.assertEqual(target, workspace / "src-tauri/target")
                candidate = target / "release/bundle/macos/CC Switch.app"
                fake_app(candidate, "new", "4.0.21")
                helper = candidate / "Contents/Resources/custom-updater.py"
                helper.parent.mkdir()
                helper.write_text("helper")
                (workspace / "dist").mkdir()
                if fail_build:
                    raise updater.UpdateError("simulated build failure")
            else:
                return self.copy_command(args)
            return b""

        def install(candidate, app, state, log):
            if fail_install:
                raise updater.UpdateError("simulated install failure")
            return original_install(candidate, app, state, log)

        def emit(stage, **extra):
            if stage == "done":
                self.assertTrue(workspaces)
                self.assertFalse(workspaces[0].exists(), "Done must follow cleanup")

        original_install = updater.install_candidate
        with patch.object(updater, "command", side_effect=run), \
                patch.object(updater, "verify_customization"), \
                patch.object(updater.shutil, "which", return_value="/fake/tool"), \
                patch.object(updater, "emit", side_effect=emit), \
                patch.object(updater, "install_candidate", side_effect=install):
            if fail_build or fail_install:
                with self.assertRaisesRegex(updater.UpdateError, "simulated"):
                    updater.update_locked(repo, "a" * 40, "refs/heads/custom",
                                          self.state, self.app, False, prepare_only, "v4.0.21")
            else:
                updated = updater.update_locked(repo, "a" * 40, "refs/heads/custom",
                                                self.state, self.app, False, prepare_only, "v4.0.21")
                self.assertEqual(updated, not prepare_only)
        self.assertFalse(workspaces[0].exists())
        self.assertEqual(cache.read_text(), "unrelated")

    def test_success_removes_build_and_dependencies_after_install(self):
        self.run_update_pipeline()
        self.assertEqual((self.app / "Contents/binary").read_text(), "new")
        receipt = json.loads((self.state / "last-update.json").read_text())
        self.assertEqual(receipt["cleanup_warnings"], [])
        self.assertEqual((Path(receipt["backup"]) / "Contents/binary").read_text(), "old")

    def test_prepare_only_keeps_only_verified_application(self):
        self.run_update_pipeline(prepare_only=True)
        self.assertEqual((self.app / "Contents/binary").read_text(), "old")
        receipt = json.loads((self.state / "prepared.json").read_text())
        candidate = Path(receipt["candidate"])
        self.assertEqual((candidate / "Contents/binary").read_text(), "new")
        self.assertTrue(candidate.is_relative_to(self.state / "prepared"))

    def test_cleanup_warning_does_not_mark_installed_update_failed(self):
        self.run_update_pipeline(fail_git_cleanup=True)
        self.assertEqual((self.app / "Contents/binary").read_text(), "new")
        receipt = json.loads((self.state / "last-update.json").read_text())
        self.assertTrue(receipt["cleanup_warnings"])
        self.assertIn("unregister failure", Path(receipt["log"]).read_text())

    def test_build_failure_cleans_generated_files(self):
        self.run_update_pipeline(fail_build=True)
        self.assertEqual((self.app / "Contents/binary").read_text(), "old")

    def test_install_failure_cleans_generated_files(self):
        self.run_update_pipeline(fail_install=True)
        self.assertEqual((self.app / "Contents/binary").read_text(), "old")

    def test_cleanup_removes_files_even_when_git_unregister_fails(self):
        workspace = self.state / "worktrees/run"
        (workspace / "target").mkdir(parents=True)
        log = self.state / "cleanup.log"
        with patch.object(updater, "command", side_effect=updater.UpdateError("git failure")):
            warnings = updater.cleanup_workspace(self.root, workspace, log)
        self.assertFalse(workspace.exists())
        self.assertTrue(warnings)
        self.assertIn("git failure", log.read_text())

    def test_cleanup_does_not_follow_replacement_symlink(self):
        workspace = self.state / "worktree"
        workspace.symlink_to(self.app, target_is_directory=True)
        with patch.object(updater, "command") as run:
            warnings = updater.cleanup_workspace(self.root, workspace, self.state / "cleanup.log")
        run.assert_not_called()
        self.assertTrue(warnings)
        self.assertEqual((self.app / "Contents/binary").read_text(), "old")

    def test_real_worktree_cleanup_removes_ignored_artifacts(self):
        repo = self.root / "source"
        repo.mkdir()
        git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "base"], check=True)
        workspace = self.state / "worktree"
        subprocess.run([*git, "worktree", "add", "--detach", str(workspace), "HEAD"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (workspace / ".gitignore").write_text("target/\nnode_modules/\ndist/\n")
        for name in ["target", "node_modules", "dist"]:
            (workspace / name).mkdir()
            (workspace / name / "generated").write_bytes(b"generated")
        self.assertEqual(updater.cleanup_workspace(repo, workspace, self.state / "cleanup.log"), [])
        self.assertFalse(workspace.exists())
        listing = subprocess.check_output([*git, "worktree", "list", "--porcelain"], text=True)
        self.assertNotIn(str(workspace), listing)

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
