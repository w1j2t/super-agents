//! 原子落盘：先写同目录 tmp 文件再 rename 替换，避免半写坏库。

use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

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

/// 每次落盘独享的临时文件名。
///
/// 曾经固定为 `<name>.tmp`：两个并发落盘会写同一个文件——A 写到一半，B 又从 0 截断重写，
/// 最后一个 rename 成功的人拿到的是两人内容的交错体，快照直接坏掉。
/// `rename` 的原子性只保证"替换"这一步不可分割，**保护不了共享的 tmp**。
/// 因此 tmp 名必须带 pid + 进程内单调序号：同进程并发、多进程同时落盘都不会撞名。
fn tmp_path(path: &Path) -> PathBuf {
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(format!(".{}.{}.tmp", std::process::id(), seq));
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

    /// 并发落盘不能出现"交错内容"。
    ///
    /// 旧实现（所有落盘共用 `db.json.tmp`）在这里会读到既不是 A 也不是 B 的字节串：
    /// 4KB 的 A 写到一半被 B 从 0 截断，长度和内容都对不上。
    #[test]
    fn concurrent_writes_never_interleave() {
        let dir = std::env::temp_dir().join(format!("sa_snap_race_{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("db.json");
        let big = vec![b'A'; 4096];
        let small = vec![b'B'; 8];

        let mut handles = Vec::new();
        for i in 0..8 {
            let p = path.clone();
            let a = big.clone();
            let b = small.clone();
            handles.push(std::thread::spawn(move || {
                for round in 0..25 {
                    let body = if (i + round) % 2 == 0 { &a } else { &b };
                    // 并发 rename 在同一目标上偶发失败是允许的（Windows 上另一个线程
                    // 刚 rename 完的瞬间可能占着句柄）；本测试只关心"不许写坏"。
                    let _ = atomic_write(&p, body);
                }
            }));
        }
        for h in handles {
            h.join().unwrap();
        }

        let got = fs::read(&path).unwrap();
        assert!(
            got == big || got == small,
            "落盘内容既不是 A 也不是 B（{} 字节）：并发落盘共用了同一个 tmp 文件",
            got.len()
        );

        let leftovers: Vec<String> = fs::read_dir(&dir)
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().to_string())
            .filter(|n| n.ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty(), "落盘后残留临时文件：{leftovers:?}");
        let _ = fs::remove_dir_all(&dir);
    }

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
