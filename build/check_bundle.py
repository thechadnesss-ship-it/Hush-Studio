"""Check a built Hush Studio folder before it goes into Setup.exe (T21).

    python build/check_bundle.py dist/HushStudio            # layout + DLL check
    python build/check_bundle.py dist/HushStudio --run      # … plus HushStudio.exe --self-test

* Layout: every file the app needs at run time is where core.config.bundle_root() expects it.
* DLLs: each bundled helper .exe (ffmpeg, ffprobe, deep-filter) may only import Windows' own
  DLLs or DLLs shipped beside it. The VC++ runtime (vcruntime/msvcp) is NOT part of a clean
  Windows 10, so it must sit next to the exe (deep-filter.exe needs VCRUNTIME140.dll).
Standard library only. Exit code 0 = good to package.
"""
from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REQUIRED = [
    "HushStudio.exe",
    "_internal/config/render.toml",
    "_internal/config/youtube_template.toml",
    "_internal/assets/fonts/Montserrat.ttf",
    "_internal/assets/fonts/OFL.txt",
    "_internal/tools/ffmpeg/ffmpeg.exe",
    "_internal/tools/ffmpeg/ffprobe.exe",
    "_internal/tools/deepfilter/deep-filter.exe",
    "_internal/tools/deepfilter/vcruntime140.dll",
    "_internal/THIRD-PARTY-NOTICES.md",
]
HELPERS = ["_internal/tools/ffmpeg/ffmpeg.exe", "_internal/tools/ffmpeg/ffprobe.exe",
           "_internal/tools/deepfilter/deep-filter.exe"]
# Redistributable runtimes a clean Windows 10/11 does not have: must be shipped beside the exe.
NOT_IN_WINDOWS = re.compile(r"^(vcruntime\d+(_\d+)?|msvcp\d+(_\w+)?|concrt\d+|vcomp\d+|libgcc.*|libstdc\+\+.*|libwinpthread.*)\.dll$", re.I)


def pe_imports(path: Path) -> list[str]:
    """DLL names from a PE file's import directory (no third-party parser needed)."""
    d = path.read_bytes()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    if d[pe:pe + 4] != b"PE\0\0":
        raise ValueError(f"{path} is not a PE file")
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    ddir = opt + (112 if magic == 0x20B else 96)
    imp_rva = struct.unpack_from("<I", d, ddir + 8)[0]
    secs = []
    for i in range(nsec):
        s = opt + optsz + 40 * i
        vsz, va, rsz, rptr = struct.unpack_from("<IIII", d, s + 8)
        secs.append((va, max(vsz, rsz), rptr))

    def off(rva: int) -> int:
        for va, size, ptr in secs:
            if va <= rva < va + size:
                return rva - va + ptr
        raise ValueError(f"RVA {rva:#x} outside sections in {path}")

    names, o = [], off(imp_rva) if imp_rva else None
    while o is not None:
        name_rva = struct.unpack_from("<I", d, o + 12)[0]
        if not name_rva:
            break
        n = off(name_rva)
        names.append(d[n:d.index(b"\0", n)].decode("ascii"))
        o += 20
    return names


def missing_runtime_dlls(exe: Path) -> list[str]:
    beside = {p.name.lower() for p in exe.parent.glob("*.dll")}
    return [n for n in pe_imports(exe) if NOT_IN_WINDOWS.match(n) and n.lower() not in beside]


def check(folder: Path, run: bool = False) -> list[str]:
    problems = [f"missing {rel}" for rel in REQUIRED if not (folder / rel).is_file()]
    for rel in HELPERS:
        exe = folder / rel
        if exe.is_file():
            for dll in missing_runtime_dlls(exe):
                problems.append(f"{rel} needs {dll}, which a clean Windows doesn't have; ship it beside the exe")
    if run and not problems:
        problems += run_self_test(folder)
    return problems


def run_self_test(folder: Path) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="hush-check-") as td:
        report_path = Path(td) / "selftest.json"
        proc = subprocess.run([str(folder / "HushStudio.exe"), "--self-test", str(report_path)], timeout=600)
        if not report_path.is_file():
            return [f"HushStudio.exe --self-test wrote no report (exit {proc.returncode})"]
        report = json.loads(report_path.read_text(encoding="utf-8"))
    for r in report["results"]:
        print(f"  {'ok  ' if r['ok'] else 'FAIL'} {r['check']}: {r['detail']}")
    bad = [f"self-test failed: {r['check']}: {r['detail']}" for r in report["results"] if not r["ok"]]
    if not report.get("frozen"):
        bad.append("self-test did not run frozen")
    if proc.returncode != 0 and not bad:
        bad.append(f"self-test exit code {proc.returncode}")
    return bad


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    problems = check(Path(argv[0]), run="--run" in argv)
    for p in problems:
        print(f"PROBLEM: {p}")
    print("bundle OK" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
