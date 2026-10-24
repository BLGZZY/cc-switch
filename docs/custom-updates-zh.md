# 本机定制版更新

在“设置 → 关于”中使用原来的“检查更新 / 更新到…”按钮。macOS 定制版会获取官方稳定版源码，在独立 Git worktree 中应用本机已提交的定制补丁，再构建、校验、备份并替换应用，最后自动重启。

菜单栏保持 13pt、居中、左右各 2pt。更新助手本身也包含在定制补丁里，因此新版仍使用这一更新流程。

补丁冲突、依赖缺失、构建或校验失败时停止；不会回退安装官方原版。源码工作区和账号配置不参与应用替换。安装失败会恢复原应用。新版本自身的运行兼容性仍需更新后检查。

## 本机配置

`~/.cc-switch/custom-updater.json` 指定 `source_repo`、`base_ref`（官方基线的完整 commit SHA）和 `custom_ref`（保存定制的本地分支引用）。只重放该分支已经提交的改动；当前定制分支有未提交的跟踪文件改动时停止更新。

不要删除源码目录，也不要删除配置中的定制分支。修改定制后，提交到该分支即可让后续更新携带新改动。更新不会修改源码目录当前检出的版本或分支。

日志与每次使用的补丁位于 `~/.cc-switch/custom-updater/logs/`，完整旧应用保存在 `~/.cc-switch/custom-updater/backups/`。`last-update.json` 记录最后一次成功安装的版本、官方提交、定制提交与备份路径。备份不自动清理。

## 手动验证

```sh
/usr/bin/python3 scripts/test_custom_updater.py
# 即使当前已是最新版，也构建并校验，但不安装：
/usr/bin/python3 scripts/custom_updater.py --rebuild --prepare-only
```

首次配置或修复助手时，需先将助手与菜单栏的全部改动提交到定制分支。GUI 启动的更新助手会补充常见的 Homebrew 和 Cargo 路径，需要本机保留 Git、Node、pnpm、Rust 和 Xcode Command Line Tools。
