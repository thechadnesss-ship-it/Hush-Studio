"""Takes library (T10): list, rename, duplicate, delete, search, export/import, status, persistence."""
import json
import os
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.takes import TakeLibrary, migrate_sessions_folder


def make_take(lib: TakeLibrary, name="", edits=False, rendered=False, secs=2.0) -> Path:
    d = lib.new_dir(name)
    sf.write(d / "raw.flac", np.zeros(int(48000 * secs), np.float32), 48000)
    sf.write(d / "clean.wav", np.zeros(int(48000 * secs), np.float32), 48000)
    if edits:
        (d / "edits.json").write_text(json.dumps({"duration": secs, "sample_rate": 48000}))
    if rendered:
        (d / "output_8h.mp4").write_bytes(b"x" * 100)
    return d


@pytest.fixture
def lib(tmp_path):
    return TakeLibrary(tmp_path / "Takes")


def test_new_take_is_listed_after_restart(lib):
    d = make_take(lib, "Deep sleep one")
    again = TakeLibrary(lib.root)                         # "reopen the app"
    (t,) = again.list()
    assert t.name == "Deep sleep one" and t.dir == d and t.status == "raw"
    assert t.length_s == pytest.approx(2.0, abs=0.01)


def test_status_progression(lib):
    d = make_take(lib)
    assert lib.list()[0].status == "raw"
    (d / "edits.json").write_text(json.dumps({"duration": 2.0}))
    assert lib.list()[0].status == "edited"
    (d / "output_8h.mp4").write_bytes(b"x")
    assert lib.list()[0].status == "rendered"
    lib.mark_uploaded(d, "https://youtu.be/x")
    assert lib.list()[0].status == "uploaded"


def test_rename_keeps_folder(lib):
    d = make_take(lib, "a")
    lib.rename(lib.list()[0], "  Better name ")
    t = lib.list()[0]
    assert t.name == "Better name" and t.dir == d
    with pytest.raises(ValueError):
        lib.rename(t, "  ")


def test_duplicate_copies_work_not_video(lib):
    make_take(lib, "orig", edits=True, rendered=True)
    lib.set_notes(lib.list()[0], "windy")
    dup = lib.duplicate(lib.list()[0])
    assert dup.name == "orig (copy)" and dup.notes == "windy"
    assert (dup.dir / "raw.flac").exists() and (dup.dir / "edits.json").exists()
    assert not dup.rendered_files and dup.status == "edited"
    assert len(lib.list()) == 2


def test_delete_removes_folder_and_refuses_outside(lib, tmp_path):
    d = make_take(lib)
    lib.delete(lib.list()[0])
    assert not d.exists() and lib.list() == []
    outside = tmp_path / "other"
    outside.mkdir()
    with pytest.raises(ValueError):
        lib.delete(outside)
    assert outside.exists()


def test_search(lib):
    make_take(lib, "Ocean waves")
    make_take(lib, "Forest")
    lib.set_notes(lib.list("forest")[0], "birds in the background")
    assert [t.name for t in lib.list("ocean")] == ["Ocean waves"]
    assert [t.name for t in lib.list("BIRDS")] == ["Forest"]
    assert lib.list("zzz") == []


def test_same_second_takes_get_distinct_folders(lib):
    a, b = make_take(lib), make_take(lib)
    assert a != b and len(lib.list()) == 2


def test_legacy_session_without_take_json_is_listed(lib):
    d = lib.root / "2026-11-01_101500"
    d.mkdir(parents=True)
    sf.write(d / "raw.flac", np.zeros(4800, np.float32), 48000)
    (t,) = lib.list()
    assert t.name == "2026-11-01_101500" and t.created.year == 2026


def test_export_import_roundtrip(lib, tmp_path):
    make_take(lib, "share me", edits=True, rendered=True)
    f = lib.export(lib.list()[0], tmp_path / "share")
    assert f.suffix == ".hushtake"
    with zipfile.ZipFile(f) as z:
        assert "raw.flac" in z.namelist() and not any("output" in n for n in z.namelist())
    other = TakeLibrary(tmp_path / "Other")
    t = other.import_export(f)
    assert t.name == "share me" and t.status == "edited" and (t.dir / "clean.wav").exists()


def test_import_rejects_junk_and_traversal(lib, tmp_path):
    bad = tmp_path / "bad.hushtake"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("hello.txt", "x")
    with pytest.raises(ValueError):
        lib.import_export(bad)
    evil = tmp_path / "evil.hushtake"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(ValueError):
        lib.import_export(evil)
    assert not (lib.root / "escape.txt").exists() and lib.list() == []


def test_show_in_explorer_targets_take_folder(lib, monkeypatch):
    d = make_take(lib)
    calls = []
    monkeypatch.setattr("subprocess.Popen", lambda cmd, *a, **k: calls.append(cmd))
    cmd = lib.show_in_explorer(lib.list()[0])
    assert calls == [cmd] and cmd[-1] == str(d)


def test_migrate_sessions_folder(tmp_path):
    (tmp_path / "sessions" / "a").mkdir(parents=True)
    takes = migrate_sessions_folder(tmp_path)
    assert takes == tmp_path / "Takes" and (takes / "a").is_dir() and not (tmp_path / "sessions").exists()
    assert migrate_sessions_folder(tmp_path) == takes            # idempotent
