#!/usr/bin/env python3
"""Build official stable releases with this machine's committed customization."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

UPSTREAM = "https://github.com/farion1231/cc-switch.git"
RELEASE_API = "https://api.github.com/repos/farion1231/cc-switch/releases/latest"
APP_ID = "com.ccswitch.desktop"


class UpdateError(RuntimeError):
    pass


def emit(stage, **extra):
    print(json.dumps(dict(stage=stage, **extra), ensure_ascii=False), flush=True)


def version(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise UpdateError("Invalid stable release version: " + str(value))
    return tuple(map(int, match.groups()))


def latest_release():
    request = urllib.request.Request(RELEASE_API, headers={
        "User-Agent": "CC-Switch-Custom-Updater", "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    if release.get("draft") or release.get("prerelease"):
        raise UpdateError("The latest release is not a stable release")
    tag = release["tag_name"]
    version(tag)
    return tag


def app_version(app):
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != APP_ID:
        raise UpdateError("Not a CC Switch application: " + str(app))
    value = info["CFBundleShortVersionString"]
    version(value)
    return value


def atomic_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def command(args, *, cwd=None, log=None, env=None, data=None, timeout=1800):
    result = subprocess.run(args, cwd=cwd, env=env, input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if log:
        with log.open("ab") as stream:
            stream.write(("\n$ " + " ".join(map(str, args)) + "\n").encode())
            stream.write(result.stdout)
    if result.returncode:
        raise UpdateError("Command failed: " + str(args[0]) +
                          ("; log: " + str(log) if log else ""))
    return result.stdout


def verify_customization(workspace, icon_digest):
    tray = (workspace / "src-tauri/src/tray.rs").read_text()
    required = [r"TRAY_TITLE_FONT_SIZE:\s*f64\s*=\s*13\.0;",
                r"TRAY_TITLE_HORIZONTAL_PADDING:\s*f64\s*=\s*4\.0;",
                r"fn style_tray_quota_title\("]
    if not all(re.search(pattern, tray) for pattern in required):
        raise UpdateError("The menu bar customization was not preserved")
    if "TRAY_TITLE_OPTICAL_OFFSET" in tray:
        raise UpdateError("Refusing the reverted text-offset customization")
    icon = workspace / "src-tauri/icons/tray/macos/statusbar_template_3x.png"
    if hashlib.sha256(icon.read_bytes()).hexdigest() != icon_digest:
        raise UpdateError("The customized menu bar icon was not preserved")
    settings = (workspace / "src-tauri/src/commands/settings.rs").read_text()
    if "custom_updater::install" not in settings:
        raise UpdateError("The custom update entry point was not preserved")
    resources = json.loads((workspace / "src-tauri/tauri.conf.json").read_text())["bundle"].get("resources", {})
    if "custom-updater.py" not in resources.values():
        raise UpdateError("The update helper is missing from bundle resources")


def install_candidate(candidate, destination, state, log):
    """Copy and verify first; restore the previous bundle if the rename fails."""
    identifier = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    stage = destination.parent / (".CC Switch.update-" + identifier + ".app")
    previous = destination.parent / (".CC Switch.previous-" + identifier + ".app")
    backup = state / "backups" / ("CC Switch-" + identifier + ".app")
    backup.parent.mkdir(parents=True, exist_ok=True)
    moved = False
    try:
        command(["/usr/bin/ditto", str(candidate), str(stage)], log=log)
        command(["/usr/bin/codesign", "--verify", "--deep", str(stage)], log=log)
        command(["/usr/bin/ditto", str(destination), str(backup)], log=log)
        command(["/usr/bin/codesign", "--verify", "--deep", str(backup)], log=log)
        os.replace(destination, previous)
        moved = True
        try:
            os.replace(stage, destination)
        except BaseException:
            os.replace(previous, destination)
            moved = False
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    if moved:
        # Only this run's temporary old bundle; the permanent backup stays intact.
        with contextlib.suppress(OSError):
            shutil.rmtree(previous)
    return backup


def cleanup_workspace(repo, workspace, log):
    """Remove only this run's worktree, including its build output and dependencies.

    Cleanup must not mask a build failure or report an already installed update as
    failed. If Git cannot unregister the worktree, still remove its large files.
    Never follow a replacement symlink into another checkout.
    """
    warnings = []
    if workspace.is_symlink():
        warnings.append("Refusing to clean a symlinked update workspace: " + str(workspace))
    else:
        try:
            command(["git", "worktree", "remove", "--force", str(workspace)],
                    cwd=repo, log=log, timeout=120)
        except (UpdateError, OSError, subprocess.TimeoutExpired) as error:
            warnings.append("Could not unregister update workspace: " + str(error))
        if workspace.exists():
            try:
                shutil.rmtree(workspace)
            except OSError as error:
                warnings.append("Could not remove update files: " + str(error))
    if warnings:
        with contextlib.suppress(OSError):
            with log.open("a") as stream:
                stream.write("\nCleanup warnings:\n" + "\n".join(warnings) + "\n")
    return warnings


def update(config_path, app, *, rebuild=False, prepare_only=False, target_tag=None):
    config = json.loads(config_path.read_text())
    repo = Path(config["source_repo"]).expanduser().resolve()
    base = config["base_ref"]
    custom = config["custom_ref"]
    if not re.fullmatch(r"[0-9a-f]{40}", base) or not custom.startswith("refs/heads/"):
        raise UpdateError("Invalid customization references in updater configuration")
    state = config_path.parent / "custom-updater"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("Another custom update is already running")
        return update_locked(repo, base, custom, state, app, rebuild, prepare_only, target_tag)


def update_locked(repo, base, custom, state, app, rebuild, prepare_only, target_tag):
    emit("checking")
    installed = app_version(app)
    tag = target_tag or latest_release()
    target = version(tag)
    if target < version(installed) or (target == version(installed) and not rebuild):
        emit("done", updated=False, version=installed)
        return False
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(Path.home() / ".cargo/bin"), "/opt/homebrew/bin",
                                    "/usr/local/bin", env.get("PATH", "/usr/bin:/bin")])
    for tool in ["git", "pnpm", "node", "cargo"]:
        if not shutil.which(tool, path=env["PATH"]):
            raise UpdateError("Required build tool is unavailable: " + tool)
    if shutil.disk_usage(state).free < 1024**3:
        raise UpdateError("At least 1 GiB of free space is needed to build the update")
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    logs = state / "logs"
    logs.mkdir(exist_ok=True)
    log = logs / (run_id + ".log")
    emit("fetching", log=str(log))
    custom_commit = command(["git", "rev-parse", "--verify", custom + "^{commit}"], cwd=repo).decode().strip()
    # Uncommitted customization would otherwise silently be omitted.
    head = command(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    if head == custom_commit:
        dirty = command(["git", "diff", "--name-only", "HEAD", "--"], cwd=repo)
        if dirty.strip():
            raise UpdateError("Commit the customization changes before updating")
    patch = command(["git", "diff", "--binary", base, custom_commit, "--"], cwd=repo)
    if not patch:
        raise UpdateError("The customization patch is empty")
    patch_digest = hashlib.sha256(patch).hexdigest()
    (logs / (run_id + ".patch")).write_bytes(patch)
    icon = command(["git", "show", custom_commit + ":src-tauri/icons/tray/macos/statusbar_template_3x.png"], cwd=repo)
    release_ref = "refs/cc-switch-updater/releases/" + tag
    command(["git", "fetch", "--no-tags", UPSTREAM,
             "refs/tags/" + tag + ":" + release_ref], cwd=repo, log=log, env=env, timeout=120)
    upstream_commit = command(["git", "rev-parse", release_ref + "^{commit}"], cwd=repo).decode().strip()
    workspace = state / "worktrees" / run_id
    workspace.parent.mkdir(exist_ok=True)
    added = False
    try:
        command(["git", "worktree", "add", "--detach", str(workspace), upstream_commit], cwd=repo, log=log)
        added = True
        emit("patching", version=tag)
        command(["git", "apply", "--3way", "--index", "-"], cwd=workspace, log=log, data=patch)
        verify_customization(workspace, hashlib.sha256(icon).hexdigest())
        declared = json.loads((workspace / "package.json").read_text())["version"]
        if version(declared) != target:
            raise UpdateError("Release tag and source version do not match")
        # Keep all generated files in the disposable worktree. Never delete or
        # reuse build output belonging to the user's source checkout.
        env["CARGO_TARGET_DIR"] = str(workspace / "src-tauri/target")
        emit("dependencies")
        command(["pnpm", "install", "--frozen-lockfile"], cwd=workspace, log=log, env=env, timeout=600)
        emit("building")
        command(["pnpm", "tauri", "build", "--bundles", "app", "--config",
                 '{"bundle":{"createUpdaterArtifacts":false}}', "--", "--locked"],
                cwd=workspace, log=log, env=env)
        candidate = Path(env["CARGO_TARGET_DIR"]) / "release/bundle/macos/CC Switch.app"
        if version(app_version(candidate)) != target:
            raise UpdateError("Built application version does not match the release")
        helper = candidate / "Contents/Resources/custom-updater.py"
        if not helper.is_file():
            raise UpdateError("Built application is missing the update helper")
        emit("verifying")
        command(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(candidate)], log=log)
        command(["/usr/bin/codesign", "--verify", "--deep", str(candidate)], log=log)
        receipt = dict(version=declared, upstream_commit=upstream_commit,
                       custom_commit=custom_commit, patch_sha256=patch_digest, log=str(log))
        if prepare_only:
            # Keep only the verified deliverable, not gigabytes of compiler cache.
            prepared_dir = state / "prepared" / run_id
            prepared_dir.mkdir(parents=True)
            prepared_app = prepared_dir / "CC Switch.app"
            try:
                command(["/usr/bin/ditto", str(candidate), str(prepared_app)], log=log)
                command(["/usr/bin/codesign", "--verify", "--deep", str(prepared_app)], log=log)
                if version(app_version(prepared_app)) != target:
                    raise UpdateError("Prepared application version does not match the release")
            except BaseException:
                shutil.rmtree(prepared_dir)
                raise
            receipt_path = state / "prepared.json"
            receipt["candidate"] = str(prepared_app)
            completion = dict(updated=False, prepared=True, candidate=str(prepared_app))
        else:
            if version(app_version(app)) != version(installed):
                raise UpdateError("The installed app changed while building; update stopped")
            emit("installing")
            backup = install_candidate(candidate, app, state, log)
            receipt_path = state / "last-update.json"
            receipt["backup"] = str(backup)
            completion = dict(updated=True, version=declared, backup=str(backup))
    finally:
        cleanup_warnings = []
        if added:
            cleanup_warnings = cleanup_workspace(repo, workspace, log)
    receipt["cleanup_warnings"] = cleanup_warnings
    with contextlib.suppress(OSError):
        atomic_json(receipt_path, receipt)
    # The UI restarts only after cleanup has finished, with success kept distinct
    # from a best-effort cleanup warning recorded in the log and receipt.
    emit("done", **completion, cleanup_warnings=cleanup_warnings)
    return completion["updated"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path.home() / ".cc-switch/custom-updater.json")
    parser.add_argument("--app", type=Path, default=Path("/Applications/CC Switch.app"))
    parser.add_argument("--rebuild", action="store_true", help="Rebuild even when the release is already installed")
    parser.add_argument("--prepare-only", action="store_true", help="Build and verify without replacing the installed app")
    args = parser.parse_args()
    try:
        update(args.config, args.app, rebuild=args.rebuild, prepare_only=args.prepare_only)
    except Exception as error:
        emit("error", message=str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
