"""把一个目录（可选叠加若干个 tar.gz 里的运行时）打成跨平台可解的 zip。

不用 Compress-Archive 的原因：Windows PowerShell 5.1 把条目名写成反斜杠，
macOS 的「归档实用工具」解出来会变成一堆名字里带 \\ 的平铺文件；
它也不记录 Unix 权限位，.command 到了 Mac 上没有可执行权限。

这里直接用标准库 zipfile：
  - 条目名统一正斜杠，顶层是源目录自己的名字；
  - 非 ASCII 名由 zipfile 自动置 UTF-8 标志位（中文名在 Mac / Linux 不乱码）；
  - 按后缀写 Unix 权限：脚本 0755，其余 0644。

「免安装版」再往里叠 Python 运行时（tar.gz 来自 python-build-standalone：
https://github.com/astral-sh/python-build-standalone/releases ，
Mac 选 cpython-3.12.x-<arch>-apple-darwin-install_only_stripped.tar.gz，
Windows 选 cpython-3.12.x-x86_64-pc-windows-msvc-install_only_stripped.tar.gz）：
  - --add-tar <tar.gz>=<前缀>：tar 顶层固定是 python/，去掉它换上前缀，
    挂到「源目录名」这一层下面（前缀相对源目录根，如 python-runtime/macos-arm64、
    python-runtime/windows-x86_64）；
  - 权限按 tar 模式规范化：有执行位 0755，否则 0644；
  - 目录与符号链接都不写（Mac 启动器直接调用 bin/python3.12，Windows 调用
    顶层 python.exe；这两个文件本身必须是普通文件，不是就直接报错）；
  - --prune <清单>：按清单裁剪（每行一个 tar 内相对路径，目录以 / 结尾），
    可传多个文件合并（Mac 与 Windows 各一份清单）；
  - 回读时额外校验：Mac 的 <前缀>/bin/python3.12 是 64 位小端 Mach-O，且
    cputype 与 macos-<arch> 目录名一致；Windows 的 <前缀>/python.exe 是
    x86_64 PE（MZ 头 + PE 签名 + 机器码 0x8664）。

用法：python zip_tree.py <源目录> <输出 zip> [--add-tar TAR.GZ=前缀 ...] [--prune 清单 ...]
"""
import argparse
import stat
import struct
import tarfile
import time
import zipfile
from pathlib import Path

SCRIPT_SUFFIXES = {".command", ".sh"}
EARLIEST = (1980, 1, 1, 0, 0, 0)     # zip 的时间戳下限
LATEST = (2107, 12, 31, 23, 59, 58)  # zip 的时间戳上限
MACHO_MAGIC = b"\xcf\xfa\xed\xfe"    # 64 位小端 Mach-O（arm64 与 x86_64 都是）
CPU_ARCH = {0x0100000C: "arm64", 0x01000007: "x86_64"}
PE_MACHINE_X64 = 0x8664              # PE 机器码：x86_64


def zip_time(epoch):
    try:
        t = time.localtime(epoch)[:6]
    except (OSError, ValueError, OverflowError):
        return EARLIEST
    return min(max(t, EARLIEST), LATEST)


def norm_mode(executable):
    return 0o755 if executable else 0o644


def is_pruned(rel, prune):
    for p in prune:
        if p.endswith("/"):
            if rel == p[:-1] or rel.startswith(p):
                return True
        elif rel == p:
            return True
    return False


def write_entry(z, arcname, data, mode, epoch):
    info = zipfile.ZipInfo(arcname, date_time=zip_time(epoch))
    info.create_system = 3  # Unix：macOS / Linux 解压时才会读下面的权限位
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    z.writestr(info, data, compresslevel=9)


def build(src, dst, add_tars, prune):
    """写入 zip，返回 {zip 内路径: 权限}，供 verify 逐条核对。"""
    expected = {}
    files = sorted(
        (p for p in src.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(src).as_posix(),
    )
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files:
            arcname = f"{src.name}/{path.relative_to(src).as_posix()}"
            mode = norm_mode(path.suffix.lower() in SCRIPT_SUFFIXES)
            write_entry(z, arcname, path.read_bytes(), mode, path.stat().st_mtime)
            expected[arcname] = mode
        for tgz, prefix in add_tars:
            with tarfile.open(tgz, "r:gz") as t:
                members = t.getmembers()
                for m in members:
                    rel = m.name[len("python/"):] if m.name.startswith("python/") else None
                    if rel in ("bin/python3.12", "python.exe") and not m.isfile():
                        raise SystemExit(f"{tgz} 里 {rel} 不是普通文件：{m.name}")
                for m in members:
                    if not m.isfile():
                        continue  # 目录与符号链接都不写进 zip
                    if not m.name.startswith("python/"):
                        raise SystemExit(f"tar 结构与预期不符（顶层应为 python/）：{m.name}")
                    rel = m.name[len("python/"):]
                    if is_pruned(rel, prune):
                        continue
                    arcname = f"{src.name}/{prefix}/{rel}"
                    if arcname in expected:
                        raise SystemExit(f"条目重复：{arcname}")
                    mode = norm_mode(bool(m.mode & 0o111))
                    write_entry(z, arcname, t.extractfile(m).read(), mode, m.mtime)
                    expected[arcname] = mode
    return expected


def verify(dst, expected):
    """写完立刻回读：条目集合、CRC、UTF-8 标志、Unix 权限、运行时 Mach-O / PE 头。"""
    with zipfile.ZipFile(dst) as z:
        broken = z.testzip()
        if broken is not None:
            raise SystemExit(f"CRC 校验失败：{broken}")
        infos = z.infolist()
        names = {i.filename for i in infos}
        if len(names) != len(infos):
            raise SystemExit("zip 里有重复条目名")
        if names != set(expected):
            missing = sorted(set(expected) - names)[:3]
            extra = sorted(names - set(expected))[:3]
            raise SystemExit(f"条目集合不对：缺 {missing}，多 {extra}")
        for i in infos:
            if i.create_system != 3:
                raise SystemExit(f"不是 Unix 条目：{i.filename}")
            if any(ord(c) > 127 for c in i.filename) and not i.flag_bits & 0x800:
                raise SystemExit(f"中文名缺 UTF-8 标志：{i.filename}")
            got = (i.external_attr >> 16) & 0o777
            want = expected[i.filename]
            if got != want:
                raise SystemExit(f"权限不对：{i.filename} 是 {oct(got)}，应为 {oct(want)}")
            if i.filename.endswith("/bin/python3.12"):
                with z.open(i.filename) as f:
                    head = f.read(8)
                if head[:4] != MACHO_MAGIC:
                    raise SystemExit(f"不是 64 位小端 Mach-O：{i.filename} 头 {head[:4].hex()}")
                cpu = struct.unpack("<I", head[4:8])[0]
                arch = CPU_ARCH.get(cpu)
                if arch is None or f"macos-{arch}" not in i.filename:
                    raise SystemExit(f"架构与目录名不符：{i.filename} cputype={cpu:#x}")
            if i.filename.endswith("/python.exe"):
                with z.open(i.filename) as f:
                    data = f.read(0x400)
                if data[:2] != b"MZ":
                    raise SystemExit(f"不是 PE（无 MZ 头）：{i.filename} 头 {data[:2].hex()}")
                off = struct.unpack_from("<I", data, 0x3C)[0]
                sig = data[off:off + 4]
                machine = struct.unpack_from("<H", data, off + 4)[0]
                if sig != b"PE\x00\x00" or machine != PE_MACHINE_X64:
                    raise SystemExit(
                        f"不是 x86_64 PE：{i.filename} sig={sig.hex()} machine={machine:#x}")


def load_prune(path):
    if path is None:
        return ()
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(line)
    return tuple(items)


def main():
    ap = argparse.ArgumentParser(description="把一个目录（可叠加 tar 运行时）打成跨平台 zip")
    ap.add_argument("src", type=Path, help="源目录")
    ap.add_argument("dst", type=Path, help="输出 zip")
    ap.add_argument("--add-tar", action="append", default=[], metavar="TAR.GZ=前缀",
                    help="把 tar.gz（顶层 python/）叠加进 zip，前缀相对源目录根，可多次")
    ap.add_argument("--prune", type=Path, action="append", default=[],
                    help="裁剪清单（tar 内相对路径），可多次，合并生效")
    a = ap.parse_args()
    if not a.src.is_dir():
        raise SystemExit(f"源目录不存在：{a.src}")
    add_tars = []
    for spec in a.add_tar:
        tgz, sep, prefix = spec.partition("=")
        if not sep or not prefix:
            raise SystemExit(f"--add-tar 要写成 <tar.gz>=<zip 内前缀>：{spec}")
        if not Path(tgz).is_file():
            raise SystemExit(f"tar 不存在：{tgz}")
        add_tars.append((Path(tgz), prefix.strip("/")))
    prune = []
    for p in a.prune:
        prune.extend(load_prune(p))
    expected = build(a.src, a.dst, add_tars, tuple(prune))
    verify(a.dst, expected)
    print(f"已写入 {len(expected)} 个条目：{a.dst}")


if __name__ == "__main__":
    main()
