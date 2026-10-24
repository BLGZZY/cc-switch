//! Machine-local source updater: failures never fall back to replacing the custom app.
#[cfg(target_os = "macos")]
pub async fn install(app: tauri::AppHandle) -> Result<bool, String> {
    use std::io::{BufRead, BufReader};
    use std::process::{Command, Stdio};
    use std::sync::atomic::{AtomicBool, Ordering};
    use tauri::{Emitter, Manager};

    static RUNNING: AtomicBool = AtomicBool::new(false);
    if RUNNING.swap(true, Ordering::AcqRel) {
        return Err("已有一个保留定制的更新任务正在运行".into());
    }
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            RUNNING.store(false, Ordering::Release);
        }
    }
    let _reset = Reset;
    let helper = app
        .path()
        .resource_dir()
        .map_err(|e| e.to_string())?
        .join("custom-updater.py");
    if !helper.is_file() {
        return Err("更新助手缺失，已停止更新以保留菜单栏定制".into());
    }
    let config = dirs::home_dir()
        .ok_or("找不到用户目录")?
        .join(".cc-switch/custom-updater.json");
    if !config.is_file() {
        return Err("缺少本地更新配置，已停止更新以保留菜单栏定制".into());
    }
    let binary = std::env::current_exe().map_err(|e| e.to_string())?;
    let bundle = binary
        .ancestors()
        .nth(3)
        .filter(|p| p.extension().is_some_and(|e| e == "app"))
        .ok_or("当前不是已安装的 macOS 应用")?
        .to_owned();
    let progress = app.clone();
    let updated = tauri::async_runtime::spawn_blocking(move || -> Result<bool, String> {
        let mut child = Command::new("/usr/bin/python3")
            .arg(helper)
            .arg("--config")
            .arg(config)
            .arg("--app")
            .arg(bundle)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("无法启动更新助手：{e}"))?;
        let stdout = child.stdout.take().ok_or("无法读取更新进度")?;
        let mut updated = None;
        let mut error = None;
        for line in BufReader::new(stdout).lines() {
            let line = line.map_err(|e| e.to_string())?;
            if let Ok(event) = serde_json::from_str::<serde_json::Value>(&line) {
                let _ = progress.emit("custom-update-progress", &event);
                match event["stage"].as_str() {
                    Some("done") => updated = event["updated"].as_bool(),
                    Some("error") => error = event["message"].as_str().map(str::to_owned),
                    _ => {}
                }
            }
        }
        let status = child.wait().map_err(|e| e.to_string())?;
        if !status.success() {
            return Err(error.unwrap_or_else(|| "更新失败，当前应用已保留".into()));
        }
        updated.ok_or_else(|| "更新助手未返回完成结果".into())
    })
    .await
    .map_err(|e| e.to_string())??;
    if !updated {
        return Ok(false);
    }
    crate::save_window_state_before_exit(&app);
    crate::cleanup_before_exit(&app).await;
    crate::restart_process(&app);
}
