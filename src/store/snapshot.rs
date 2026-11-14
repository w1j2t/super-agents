//! 原子落盘：先写同目录 tmp 文件再 rename 替换，避免半写坏库。

use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

/// 原子替换写入。成功返回写入字节数。
pub fn atomic_write(path: &Path, bytes: &[u8]) -> io::Result<usize> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let tmp = tmp_path(path);
    {
        let mut file = File::create(&tmp)?;
        file.write_all(bytes)?;
        file.flush()?;
        file.sync_all()?;
    }
    // Windows 的 std::fs::rename 使用 MOVEFILE_REPLACE_EXISTING，可覆盖同名文件
    match fs::rename(&tmp, path) {
        Ok(()) => Ok(bytes.len()),
        Err(e) => {
            let _ = fs::remove_file(&tmp);
            Err(e)
        }
    }
}

fn tmp_path(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".tmp");
    path.with_file_name(name)
}

/// 把损坏的快照另存留档，返回存档路径。
pub fn quarantine(path: &Path) -> io::Result<PathBuf> {
    let stamp = chrono::Utc::now().format("%Y%m%dT%H%M%S");
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(format!(".corrupt-{stamp}"));
    let target = path.with_file_name(name);
    fs::rename(path, &target)?;
    Ok(target)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_and_replaces() {
        let dir = std::env::temp_dir().join(format!("sa_snap_{}", uuid::Uuid::new_v4()));
        let path = dir.join("db.json");
        atomic_write(&path, b"one").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"one");
        atomic_write(&path, b"two-longer").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"two-longer");
        assert!(!path.with_file_name("db.json.tmp").exists());
        let _ = fs::remove_dir_all(&dir);
    }
}
