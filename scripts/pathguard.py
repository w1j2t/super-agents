"""删除前的路径守卫（SA-031）。

背景：`sync_build.py` / `verify.py` / `smoke.py` 各有一句 `shutil.rmtree(dst)`，
dst 由 `--build-dir` / `--data-dir` 拼出来。这套参数平时指镜像目录，一切正常；
但只要写错一位——`--build-dir` 指到源码仓库本身、`--data-dir` 指到项目根——
那句 rmtree 就不是"清缓存"而是"删源码"，而且不经回收站。
错误代价与参数的手滑概率完全不成比例，所以删除动作不能只依赖调用方自觉。

守卫口径（全部通过才允许删）：
  1. 必须是绝对路径；先 resolve，符号链接按真实目标判定；
  2. 不得是盘符根 / 用户主目录 / 系统目录（Windows、Program Files*、ProgramData）；
  3. 不得等于、不得是源码仓库的祖先、也不得位于源码仓库之内
     （第三条正是那手一滑的场景：镜像指进仓库，dst 就变成了真源码）；
     唯一例外是仓库内的产物目录（target/、temp/）及其子路径——门禁自己就要清
     target/soak 这类数据目录，一刀切只会逼着大家把守卫拆掉；
  4. 必须严格位于调用方声明的 base 之下（不等于 base，至少深一层）；
  5. base 自身同样要过第 2、3 条（否则 path 合法、base 非法，等于没守）；
  6. 若给了 allow_names / allow_prefix，名字必须命中（清缓存只清自己那几样）。

拒绝时抛 GuardError，由调用方决定退出还是降级为"跳过清理"——守卫本身绝不
静默放行。自检：`python scripts/pathguard.py --selftest`（门禁 29 调用它）。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home().resolve()


class GuardError(RuntimeError):
    """删除目标未通过守卫。消息里必须写清"哪条口径被违反"，不让调用方猜。"""


def _system_roots() -> list[Path]:
    roots: list[Path] = []
    for env in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        v = os.environ.get(env)
        if v:
            p = Path(v)
            if p.is_absolute():
                roots.append(p)
    roots.append(Path("C:/Windows"))
    roots.append(Path("C:/Program Files"))
    roots.append(Path("C:/Program Files (x86)"))
    roots.append(Path("C:/ProgramData"))
    out: list[Path] = []
    for p in roots:
        try:
            out.append(p.resolve())
        except OSError:
            continue
    return out


def _is_within(child: Path, parent: Path) -> bool:
    """child 是否在 parent 之下（含相等）。Python 3.8 兼容写法。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# 仓库内允许整体清空的目录（构建产物/临时产物）。只有这些首段的子路径能过守卫：
# 门禁自身要清 target/soak、temp/drill_* 之类；源码目录一律不在名单里。
IN_REPO_ARTIFACT_DIRS = ("target", "temp")


def _artifact_subpath(p: Path, root: Path) -> Path | None:
    """p 在 root 之下且首段是产物目录时返回相对路径，否则 None。"""
    try:
        rel = p.relative_to(root)
    except ValueError:
        return None
    return rel if rel.parts and rel.parts[0] in IN_REPO_ARTIFACT_DIRS else None


def danger_reason(path, *, base, allow_names=None, allow_prefix=None) -> str | None:
    """返回拒绝理由；None 表示允许删除。path 不存在时返回 None（无物可删）。"""
    p = Path(path)
    if not p.is_absolute():
        return f"不是绝对路径：{p}"
    try:
        rp = p.resolve()
    except OSError as exc:
        return f"无法解析路径：{p}（{exc}）"
    if not rp.exists():
        return None

    b = Path(base)
    if not b.is_absolute():
        return f"base 不是绝对路径：{b}"
    rb = b.resolve()

    # 1. 盘符根
    if rp.parent == rp:
        return f"目标是根目录：{rp}"
    # 2. 用户主目录 / 系统目录
    if rp == HOME:
        return f"目标是用户主目录：{rp}"
    if _is_within(HOME, rp):
        return f"目标是用户主目录的祖先：{rp}"
    for s in _system_roots():
        if rp == s or _is_within(rp, s):
            return f"目标落在系统目录：{rp}（系统根 {s}）"
    # 3. 源码仓库：等于 / 祖先 / 内部，三者都不许。
    # 唯一的例外是构建产物目录（target/、temp/）：门禁自己就要清 target/soak 这类
    # 数据目录，一刀切会把"正常清理"也拒掉，结果大家只能把守卫删掉来干活。
    # 放宽的只有"产物目录之下"这一层，源码目录（src/scripts/workers/docs）照旧拒绝。
    if rp == ROOT:
        return f"目标是源码仓库本身：{rp}"
    if _is_within(ROOT, rp):
        return f"目标是源码仓库的祖先：{rp}（删它会连源码一起删）"
    if _is_within(rp, ROOT) and _artifact_subpath(rp, ROOT) is None:
        return f"目标位于源码仓库之内：{rp}（仓库内只有 {'/'.join(IN_REPO_ARTIFACT_DIRS)}/ 这类产物目录可清）"
    # 4/5. base 关系
    if rb == rb.parent:
        return f"base 是根目录：{rb}"
    if rb == HOME or _is_within(HOME, rb):
        return f"base 是用户主目录或其祖先：{rb}"
    for s in _system_roots():
        if rb == s:
            return f"base 是系统目录：{rb}"
    if _is_within(rb, ROOT) and _artifact_subpath(rb, ROOT) is None:
        return f"base 位于源码仓库之内：{rb}（仓库内只有产物目录可作 base）"
    if rp == rb:
        return f"目标等于 base：{rp}（不允许整根清空）"
    if not _is_within(rp, rb):
        return f"目标不在 base 之下：{rp}（base {rb}）"
    # 6. 名字名单
    if allow_names is not None and rp.name not in set(allow_names):
        return f"目标名不在白名单：{rp.name}（允许 {sorted(allow_names)}）"
    if allow_prefix is not None and not rp.name.startswith(allow_prefix):
        return f"目标名不以 {allow_prefix!r} 开头：{rp.name}"
    return None


def check(path, *, base, allow_names=None, allow_prefix=None, what: str = "清理") -> bool:
    """只判定不删除。允许返回 True，拒绝抛 GuardError。"""
    why = danger_reason(path, base=base, allow_names=allow_names, allow_prefix=allow_prefix)
    if why:
        raise GuardError(f"[{what}] 拒绝删除：{why}")
    return True


def safe_rmtree(path, *, base, allow_names=None, allow_prefix=None, what: str = "清理",
                missing_ok: bool = True, warn_only: bool = False) -> bool:
    """过守卫才删；已被解析为符号链接的只unlink链接本身，不跟进去删目标。

    warn_only=True 用于 finally 里的"清理尽力而为"：守卫拒绝时只打印警告并返回 False。
    否则收尾异常会盖掉整轮已经跑出来的真实结论（本来是绿的，或本来是红的），
    排查的人只会看到一条路径错误，看不到真正的判定。
    """
    p = Path(path)
    if not p.exists() and missing_ok:
        return False
    try:
        check(p, base=base, allow_names=allow_names, allow_prefix=allow_prefix, what=what)
    except GuardError as e:
        if not warn_only:
            raise
        print(f"[WARN] {e}（已跳过清理，不影响本次判定）")
        return False
    if p.is_symlink():
        p.unlink()
        return True
    shutil.rmtree(p)
    return True


# ───────────────────────── 自检 ─────────────────────────
def _selftest(tmp: Path) -> int:
    ok = fail = 0

    def expect(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"[OK  ] {name}")
        else:
            fail += 1
            print(f"[FAIL] {name}  {detail}")

    def refused(name: str, path, **kw) -> None:
        why = danger_reason(path, **kw)
        expect(name, why is not None, f"本应拒绝却放行了：{path}")

    def allowed(name: str, path, **kw) -> None:
        why = danger_reason(path, **kw)
        expect(name, why is None, f"本应放行却被拒：{why}")

    mirror = tmp / "mirror"
    (mirror / "src").mkdir(parents=True)
    (mirror / "target-gnu" / "debug").mkdir(parents=True)
    (tmp / "repo-copy" / "src").mkdir(parents=True)
    (tmp / "sa-smoke-123").mkdir(parents=True)
    (tmp / "whatever").mkdir(parents=True)

    # 放行侧
    allowed("P1 允许清镜像子目录", mirror / "src", base=mirror, allow_names={"src", "scripts"})
    allowed("P2 允许清不存在目标", mirror / "gone", base=mirror)
    allowed("P3 允许清 smoke 临时目录", tmp / "sa-smoke-123", base=tmp, allow_prefix="sa-smoke-")
    # 拒绝侧：手一滑的几种真实写法
    refused("R1 拒绝源码仓库本身", ROOT, base=tmp)
    refused("R2 拒绝源码仓库祖先", ROOT.parent, base=tmp)
    refused("R3 拒绝仓库内目录（源码）", ROOT / "src", base=ROOT / "src")
    refused("R4 拒绝目标等于 base", mirror, base=mirror)
    refused("R5 拒绝盘符根", Path(ROOT.anchor), base=ROOT.anchor)
    refused("R6 拒绝用户主目录", HOME, base=HOME.parent)
    refused("R7 拒绝系统目录", Path("C:/Windows"), base="C:/")
    refused("R8 拒绝 base 在仓库内", ROOT / "scripts", base=ROOT / "scripts",
            allow_names={"scripts"})
    refused("R9 拒绝不在 base 之下", tmp / "whatever", base=mirror)
    refused("R10 拒绝名字不在白名单", mirror / "target-gnu", base=mirror, allow_names={"src"})
    refused("R11 拒绝前缀不符", tmp / "whatever", base=tmp, allow_prefix="sa-smoke-")
    refused("R12 拒绝相对路径", Path("src"), base=mirror)
    # 仓库内产物目录：门禁自己要清的就是这一类，必须放行；源码目录照旧拒绝。
    # 这条是这轮补的：口径一旦"仓库内一律拒绝"，真跑起来 target/soak 就清不掉，
    # 下一个人不会去改口径，只会把这句守卫注释掉——守卫要么好用，要么会被拆掉。
    artifact = ROOT / "temp" / f"pathguard-selftest-{os.getpid()}"
    artifact.mkdir(parents=True, exist_ok=True)
    allowed("P4 允许清仓库内产物目录（temp/ 之下）", artifact, base=ROOT / "temp",
            allow_prefix="pathguard-selftest-")
    refused("R13 否决仓库内源码目录（target 作 base）", ROOT / "src", base=ROOT / "target")
    refused("R14 否决仓库内源码目录（base 也是源码）", ROOT / "scripts", base=ROOT / "scripts")
    # 真删演练：**只在临时目录里演**。绝不拿真源码当靶子——守卫若有 bug，
    # 自检本身就成了那个"一个参数写错就删掉源码"的缺陷。
    try:
        safe_rmtree(tmp / "sa-smoke-123", base=tmp, allow_prefix="sa-smoke-")
        expect("D1 合法目标真被删除", not (tmp / "sa-smoke-123").exists())
    except GuardError as exc:
        expect("D1 合法目标真被删除", False, str(exc))
    try:
        safe_rmtree(mirror, base=mirror, what="演练")
        expect("D2 非法目标必须抛 GuardError", False, "竟然删了")
    except GuardError:
        expect("D2 非法目标必须抛 GuardError", True)
    expect("D3 非法删除未执行（靶目录仍在）", mirror.exists())
    expect("D4 源码目录未被破坏", (ROOT / "src").exists())
    # 自检自己造出来的产物目录要顺手收回，否则每次跑门禁都在 temp/ 里留一个空壳
    try:
        safe_rmtree(artifact, base=ROOT / "temp", allow_prefix="pathguard-selftest-",
                    what="自检产物")
        expect("D5 产物目录能经守卫清掉（放行侧也要真删过）", not artifact.exists())
    except GuardError as exc:
        expect("D5 产物目录能经守卫清掉（放行侧也要真删过）", False, str(exc))

    print(f"\n=== 小结：断言 {ok + fail} 项 | 通过 {ok} | 失败 {fail} ===")
    return 0 if fail == 0 else 1


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="sa-pathguard-") as td:
            return _selftest(Path(td))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
