"""T21: installer support: data folder outside the install, bundled tools, mic check, build files."""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

from core import config, ffmpeg_tools as ff, mic_check

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))
import check_bundle  # noqa: E402


# --------------------------------------------------------------------------- data + program folders

def test_data_root_dev_is_repo():
    assert config.resolve_data_root({}, frozen=False, project_root=ROOT) == ROOT


def test_data_root_installed_is_localappdata(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    assert config.resolve_data_root(env, frozen=True) == tmp_path / "HushStudio"


def test_data_root_env_override_wins(tmp_path):
    env = {"HUSH_DATA_DIR": str(tmp_path / "usb"), "LOCALAPPDATA": str(tmp_path)}
    assert config.resolve_data_root(env, frozen=True) == tmp_path / "usb"
    assert config.resolve_data_root(env, frozen=False) == tmp_path / "usb"


def test_installed_data_root_matches_log_folder(tmp_path, monkeypatch):
    """Takes and logs share %LOCALAPPDATA%\\HushStudio; the uninstaller touches neither."""
    from core import log_setup
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert log_setup.log_dir().parent == config.resolve_data_root({"LOCALAPPDATA": str(tmp_path)}, frozen=True)


def test_frozen_bundle_root_and_ffmpeg_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert config.bundle_root() == tmp_path
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    assert ff.bundled_tool_path("ffmpeg") == tmp_path / "tools" / "ffmpeg" / exe


def test_frozen_missing_ffmpeg_says_reinstall(monkeypatch):
    monkeypatch.setattr(ff, "FROZEN", True)
    monkeypatch.setattr(ff, "_candidates", lambda name: iter(()))
    ff.find_tool.cache_clear()
    try:
        with pytest.raises(ff.FFmpegError, match="installer again"):
            ff.find_tool("ffprobe")
    finally:
        ff.find_tool.cache_clear()


def test_dev_bundle_root_is_repo():
    assert config.bundle_root() == ROOT
    assert config.DEFAULT_RENDER_TOML == ROOT / "config" / "render.toml"


# --------------------------------------------------------------------------- first-run mic check

def _reg(values: dict):
    return lambda hive, path, name: values.get((hive, path))


K = mic_check.CONSENT_KEY


@pytest.mark.parametrize("values, problem", [
    ({}, ""),
    ({("HKCU", K): "Allow", ("HKCU", K + r"\NonPackaged"): "Allow"}, ""),
    ({("HKLM", K): "Deny"}, "device_off"),
    ({("HKCU", K): "Deny"}, "apps_off"),
    ({("HKCU", K + r"\NonPackaged"): "Deny"}, "desktop_apps_off"),
    ({("HKLM", K): "Deny", ("HKCU", K): "Deny"}, "device_off"),
])
def test_privacy_switches(values, problem):
    assert mic_check.privacy_problem(_reg(values)) == problem


def test_blocked_mic_is_not_opened():
    def listen():
        raise AssertionError("must not open the mic when Windows blocks it")
    r = mic_check.check_microphone(_reg({("HKCU", K + r"\NonPackaged"): "Deny"}), listen)
    assert not r.ok and r.problem == "desktop_apps_off"
    assert "desktop apps" in r.advice and r.settings_uri == mic_check.SETTINGS_URI


def test_working_mic():
    rng = np.random.default_rng(1)
    r = mic_check.check_microphone(_reg({}), lambda: ("USB PnP", rng.normal(0, 0.002, 24000).astype(np.float32)))
    assert r.ok and r.device == "USB PnP" and -60 < r.peak_dbfs < -40


def test_digital_zeros_mean_blocked_or_muted():
    r = mic_check.check_microphone(_reg({}), lambda: ("Mic", np.zeros(24000, np.float32)))
    assert not r.ok and r.problem == "silent" and "muted" in r.advice


def test_leading_zero_buffers_are_ignored():
    x = np.zeros(24000, np.float32)
    x[5000:] = 1e-4
    assert mic_check.classify_samples(x)[0]


def test_no_device_and_open_failure():
    def none():
        raise mic_check.NoInputDevice("no default input device")

    def busy():
        raise RuntimeError("Error opening InputStream: Device unavailable")
    a = mic_check.check_microphone(_reg({}), none)
    b = mic_check.check_microphone(_reg({}), busy)
    assert a.problem == "no_device" and a.settings_uri == mic_check.SOUND_SETTINGS_URI
    assert b.problem == "open_failed" and "Device unavailable" in b.detail


def test_every_problem_has_plain_advice():
    for key, (title, advice) in mic_check.MESSAGES.items():
        assert title and "Check again" in advice, key


# --------------------------------------------------------------------------- build files

ISS = (ROOT / "build" / "hush_studio.iss").read_text(encoding="utf-8")
SPEC = (ROOT / "build" / "hush_studio.spec").read_text(encoding="utf-8")


def _iss_value(key: str) -> str:
    m = re.search(rf"^{key}=(.*)$", ISS, re.M)
    assert m, key
    return m.group(1).strip()


def test_installer_is_per_user_and_named_by_version():
    assert _iss_value("PrivilegesRequired") == "lowest"
    assert _iss_value("DefaultDirName").startswith("{autopf}")
    assert _iss_value("OutputBaseFilename") == "Hush-Studio-Setup-{#AppVersion}"
    assert re.fullmatch(r"\{\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}", _iss_value("AppId"))


def test_installer_shortcuts_and_mutex():
    assert "{autoprograms}\\{#AppName}" in ISS and "{autodesktop}\\{#AppName}" in ISS
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    mutex = re.search(r'APP_MUTEX = "([^"]+)"', main_src).group(1)
    assert _iss_value("AppMutex") == mutex


def test_installer_never_deletes_user_data():
    """Every delete entry stays inside {app}; data lives in %LOCALAPPDATA%\\HushStudio."""
    deletes = re.findall(r'^Type:\s*\w+;\s*Name:\s*"([^"]+)"', ISS, re.M)
    assert deletes and all(d.startswith("{app}\\") for d in deletes)
    assert "HushStudio\\" not in "".join(deletes) and "{localappdata}" not in ISS.lower().replace("%localappdata%", "")


def test_spec_bundles_what_the_app_looks_for():
    for dest in ("config", "assets/fonts", "tools/ffmpeg", "tools/deepfilter"):
        assert f'"{dest}")' in SPEC, dest
    assert "vcruntime140.dll" in SPEC and "console=False" in SPEC and 'name="HushStudio"' in SPEC
    for rel in check_bundle.REQUIRED:
        assert rel == "HushStudio.exe" or rel.startswith("_internal/")


def test_check_bundle_reports_missing_layout(tmp_path):
    problems = check_bundle.check(tmp_path)
    assert len(problems) == len(check_bundle.REQUIRED)


@pytest.mark.skipif(sys.platform != "win32", reason="PE files")
def test_vc_runtime_must_sit_beside_helper_exe(tmp_path):
    exe = tmp_path / "python.exe"
    shutil.copy2(Path(sys.base_prefix) / "python.exe", exe)   # the real one, not the venv launcher
    imports = [n.lower() for n in check_bundle.pe_imports(exe)]
    assert "kernel32.dll" in imports
    if "vcruntime140.dll" not in imports:
        pytest.skip("this python.exe doesn't link the VC runtime")
    assert check_bundle.missing_runtime_dlls(exe) == ["VCRUNTIME140.dll"]
    shutil.copy2(Path(sys.base_prefix) / "vcruntime140.dll", tmp_path / "vcruntime140.dll")
    assert check_bundle.missing_runtime_dlls(exe) == []


@pytest.mark.skipif(not (ROOT / "tools" / "deepfilter" / "deep-filter.exe").is_file(), reason="deep-filter.exe not installed")
def test_deep_filter_needs_vcruntime_on_clean_windows():
    """Why the spec copies vcruntime140.dll next to deep-filter.exe."""
    names = [n.lower() for n in check_bundle.pe_imports(ROOT / "tools" / "deepfilter" / "deep-filter.exe")]
    assert "vcruntime140.dll" in names


@pytest.mark.slow
def test_self_test_passes_from_source(tmp_path):
    from core import selftest
    report = selftest.run_self_test(tmp_path)
    failed = [r for r in report["results"] if not r["ok"] and r["check"] != "deep-filter.exe"]
    assert report["ok"] and not failed, failed
    assert report["frozen"] is False
