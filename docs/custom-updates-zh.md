# 本机定制版更新

在“设置 → 关于”中使用原来的“检查更新 / 更新到…”按钮。macOS 定制版会获取官方稳定版源码，在独立 Git worktree 中应用本机已提交的定制补丁，再构建、校验、备份并替换应用，最后自动重启。

菜单栏保持 13pt、居中、左右各 2pt。更新助手本身也包含在定制补丁里，因此新版仍使用这一更新流程。

补丁冲突、依赖缺失、构建或校验失败时停止；不会回退安装官方原版。源码工作区和账号配置不参与应用替换。安装失败会恢复原应用。新版本自身的运行兼容性仍需更新后检查。

## 日常操作

1. 打开 CC Switch → 设置 → 关于。
2. 点“检查更新”。没有新版会提示“已是最新”；有新版会显示“更新到 v…”按钮。
3. 点“更新到 v…”，等待获取源码、保留菜单栏样式、构建、校验、备份和安装完成。更新期间保持应用开启，完成后会自动重启。
4. 若提示补丁冲突或构建失败，当前应用保持原样；错误信息会给出日志位置。需要适配时先修复定制分支、提交，再重试。

## GitHub 备份与恢复

定制源码保存于 <https://github.com/BLGZZY/cc-switch/tree/codex/macos-quota-tray>，分支是 `codex/macos-quota-tray`。GitHub 保存源码和操作说明，本机账号数据库、访问凭据、日志、构建产物和应用备份不纳入提交。

本机以后修改定制代码并提交后，可用 `git push origin codex/macos-quota-tray` 继续备份。

如需在另一台**已安装 CC Switch** 的 Mac 恢复，先准备 Git、Node、pnpm、Rust 和 Xcode Command Line Tools，再执行：

```sh
git clone --branch codex/macos-quota-tray https://github.com/BLGZZY/cc-switch.git "$HOME/cc-switch"
cd "$HOME/cc-switch"
/usr/bin/python3 - <<'PY'
import json
from pathlib import Path
config = Path.home() / ".cc-switch/custom-updater.json"
if config.exists():
    raise SystemExit("已有更新配置，请先检查，避免覆盖。")
config.parent.mkdir(parents=True, exist_ok=True)
config.write_text(json.dumps({
    "schema_version": 1,
    "source_repo": str(Path.cwd()),
    "base_ref": "b76426741ef93069e2f374ac01ca7ef5760e1d0e",
    "custom_ref": "refs/heads/codex/macos-quota-tray"
}, indent=2) + "\n")
config.chmod(0o600)
PY
```

然后先退出 CC Switch，再执行 `/usr/bin/python3 scripts/custom_updater.py --rebuild`。构建安装完成后，从“应用程序”重新打开 CC Switch；以后即可使用内置更新按钮。

## 本机配置

`~/.cc-switch/custom-updater.json` 指定 `source_repo`、`base_ref`（官方基线的完整 commit SHA）和 `custom_ref`（保存定制的本地分支引用）。只重放该分支已经提交的改动；当前定制分支有未提交的跟踪文件改动时停止更新。

不要删除源码目录，也不要删除配置中的定制分支。修改定制后，提交到该分支即可让后续更新携带新改动。更新不会修改源码目录当前检出的版本或分支。

日志与每次使用的补丁位于 `~/.cc-switch/custom-updater/logs/`，完整旧应用保存在 `~/.cc-switch/custom-updater/backups/`。`last-update.json` 记录最后一次成功安装的版本、官方提交、定制提交与备份路径。备份不自动清理。

每次更新都在独立临时工作区中安装依赖和编译。安装成功后，会在显示完成并重启前删除该工作区，包括 Rust `target`、`node_modules`、前端 `dist` 和临时安装包；更新失败也会清理这些文件。源码目录中已有的编译缓存、共享包管理器缓存、账号数据、日志和回滚备份不会被删除。下次更新会重新编译。

`--prepare-only` 只保留校验完成的应用包，路径写入 `prepared.json`，其余编译文件同样清理。若系统阻止清理，原因会写入本次日志和结果记录的 `cleanup_warnings`；已经成功安装的更新仍返回成功。

## 手动验证

```sh
/usr/bin/python3 scripts/test_custom_updater.py
# 即使当前已是最新版，也构建并校验，但不安装：
/usr/bin/python3 scripts/custom_updater.py --rebuild --prepare-only
```

首次配置或修复助手时，需先将助手与菜单栏的全部改动提交到定制分支。GUI 启动的更新助手会补充常见的 Homebrew 和 Cargo 路径，需要本机保留 Git、Node、pnpm、Rust 和 Xcode Command Line Tools。
