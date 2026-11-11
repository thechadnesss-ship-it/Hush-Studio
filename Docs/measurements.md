# Hush Studio: measurements and implementation status

> Moved out of `CLAUDE.md` section 0.6 on 2026-11-11 to keep the architecture file short (agents read CLAUDE.md at the start of every
> task; this file is read only when a task's `context` names it). Facts learned from measurements and decisions go here, under the
> matching bullet. `CLAUDE.md` stays the source of truth for architecture and product rules.

## 0.6 Implementation status & measured decisions (2026-11-09)

Built and tested (`pytest`, 171 tests): renderer + native stitcher, asset cache (intro card +
black loop), mastering, edit list, VAD, plane ear + splicer, sky radar + guard, denoise
(DeepFilterNet + gate), auto-editor, session pipeline, live capture, PyQt6 app (`python main.py`).
Also built: quiet-time planner (logger + forecast + heatmap; `planner_logger.py` runs headless),
YouTube publisher (own resumable-upload client, tested against a fake server; real Google account
not yet tested), listen-back while paused. App name lives in `core/app_info.py` (**Hush Studio**,
renamed 2026-11-09; the folder stays `pika_meditate` because the venv paths depend on it).
Decided against a live headphone monitor: 100 ms blocks + filtering ≈ 150 ms self-delay, which
makes speaking hard → "Listen back (last 15 s)" while paused instead.

- **Video intro:** `AssetCache.intro_video()` re-encodes the card once with the same x264 recipe
  as the black loop → byte-identical avcC → one H.264 track. Black frames are re-timed to 1 s
  each (`black_frame_s`, VFR) → 8 h output ≈ 155 MB instead of ≈ 218 MB at 30 fps.
- **Real-asset render:** 33:14 take + 30 s card → exact 8h 00m 00s in ≈ 5 s (incl. tail encode).
- **Mastering:** vectorised BS.1770 meter (pyloudnorm is ~20 s per pass on 30 min). Digital-silence
  pauses push IIR filters into denormals (~100× slower) → a −200 dB dither avoids it. Sam's edit
  needs < 1 dB of gain; mastering 33 min ≈ 3 s.
- **Ear (plane detector):** band 50–2000 Hz; noise = 20th-percentile of the 64 ms spectrum
  (between her voice harmonics) + 1 s minimum statistics, gate-aware (ignores < −90 dBFS
  sub-frames). Thresholds measured on her real take: RISING +15 dB/2 s, RED +25 dB (or within
  12 dB of her voice)/1 s, CLEAR = 6 s median within +6 dB. Doc's original "within 12 dB of
  speech" alone missed planes 20–30 dB under her voice, which are still audible in a hushed take.
  Per-octave estimation was tried and rejected (false alarms from her voice's fundamental).
- **Splicer:** cut starts from the noise onset (ear "rising"), at the pause before it; RED in a
  pause → cut from where she stopped talking.
- **Radar:** adsb.lol live-verified. FACT 01/19 true heading 346° (OurAirports). Unknown altitude
  (common for helicopters) → assume 1000 ft. Only approaching aircraft raise AMBER.
- **Auto-editor (`core/auto_edit.py`):** Sam says a **spoken pause-marker word** (~1.1 s) wherever a
  long silence belongs, ~90× per 33 min take; her edit replaces each with ≈ 10 s of silence. The
  markers are found per session without a template (the one short isolated utterance repeating
  near-identically: 0.86 similarity vs 0.42 for normal speech). Other pauses keep their length but
  become digital silence; lead 0.1 s, tail ≤ 13 s, cap 15 s. On her raw take: 91 markers, 72%
  digital silence (her edit: 71%). Induction markers from 180 s through 720 s use a measured 5 s
  replacement on the real take (11 markers); the remaining 80 markers keep the measured 10 s
  replacement. Short/synthetic takes retain the normal marker behavior. Flub cuts remain manual
  (Sam does those in review).
- **FINISH speed on her real take (37:30 edited → exact 8 h, 4-core i5-6500): ≈ 15 s.** Parallel AAC
  encode split at digital-silence pauses (5.5 s vs 11.3 s), and an encode-free `exact` tail: the
  session's first frames up to a ≥ 1 s pause, padded with cached silence frames, so the video ends
  in a sentence break at exactly the target length. Import-only costs (live REC does these during
  recording): noise removal ≈ 20 s and the ear ≈ 14 s for 33 min.
- **Flub proposals (T4):** `auto_edit.find_flubs` proposes a cut where a ≥ 1.5 s phrase is said again within 2 phrases / 8 s
  (feature similarity ≥ 0.88; threshold unmeasured on real takes). They are `Cut(reason="flub", proposed=True, keep=True)`:
  listed in review, **not applied** until Sam presses Accept (`keep=False`) or Undo; render honours `keep`. Plane cuts win on overlap.
- **Denoise:** `deepfilternet` pip has no Py3.12 Windows wheel → the official `deep-filter.exe`
  (v0.5.6, 26 912 256 bytes, sha256 75e11fa1…7915) in `tools/deepfilter/` (gitignored; installed by
  the user). On Sam's real voice + synthetic noise: plane in pauses −33 dB (gate −15), plane under
  her voice −6.3 dB (gate −3.7), voice −0.3 dB. It outputs 16-bit, ~30 ms short → padded. Neural:
  **never test it on synthetic voices** (it removes them). 83 s/33 min single-process → split at
  pauses on all cores = 44 s; during live REC, finished ~60 s segments run in the background.
  Built-in `SpectralGate` fallback (vectorised, exact for any chunk size): ≈ 17 dB steady noise.
- **`deep-filter.exe` quirks (T7, 2026-11-10; source of the "crackle" report):** (1) it outputs exact zeros for
  any frame under ≈ −72 dBFS **and for the 30 ms of real audio before it** (delay compensation). On Sam's
  gated Riverside take that blanked 88% of 384 phrase endings and left **370 dropouts** (5–60 ms of digital
  zero inside sound) in clean.wav → 232 in session.m4a → 247 in the MP4; raw had 0. Fix: a −65 dBFS white
  floor on its input (the model removes it; pauses come out ≈ −97 dBFS) → 0 dropouts at every stage.
  (2) Output is 1440 samples + the last partial 480-hop short → each part/segment ended in 30–40 ms of
  zeros; now fed a 0.1 s reflected tail (zeros or quiet noise trigger (1)). (3) Each part restarted cold
  (up to 30 dB low for its first frames) → 3 s of pre-roll context, also across live REC segments: a
  mid-word split now matches a whole-file run to ≤ −44 dB. (4) 16-bit output hard-clips input above
  ±1.0 (WASAPI float can exceed it) → scaled into range and back. Voice fidelity on clean input: 1.6 dB
  log-spectral distance, HF unchanged. Auto-edit on her take after the fix: 91 markers, 72.6% silence.
- **Capture diagnostics (T7):** every live take writes `capture.json` (device, host API, peak, clipped
  samples, samples beyond full scale, input overflows, dropped blocks). `python -m core.diagnostics
  sessions/<ts>` prints peak / LUFS / clipped / flat tops / dropouts per stage (raw → clean → m4a → MP4).
  The UI's mic picker opens WASAPI devices (float, can exceed ±1.0); "Default microphone" opens MME (16-bit).
  The USB PnP mic here: speech ≈ −40 LUFS at normal distance, room ≈ −56 dBFS → ~15 dB SNR, +24 dB make-up.
  UI: the level meter turns fully red for 2 s after any clipped block (peak tick amber within 6 dB), and a
  red line under it says to turn the mic level down; lost input buffers get their own warning there.
  Review (T8): live takes show a "Recording check" line from capture.json: lost-sound count with a ▶
  per spot (`lost_at_s`, take time, merged within 0.5 s, max 200) and clipping, or a green all-clear.
- **Logging (T19, `core/log_setup.py`):** `main.py` calls `setup_logging()` first: rotating `hush-studio.log` (2 MB × 5) in
  `%LOCALAPPDATA%\HushStudio\logs`, loggers `hush.{app,capture,denoise,render,upload}`, a redacting filter (tokens, secrets,
  upload ids, tracebacks included), startup report (app/Windows/Python/Qt/FFmpeg versions, input devices + host API), and a
  global `sys`/`threading` excepthook that logs the traceback and shows a friendly box. Per-take `capture.json` is unchanged.
- **Debug bundle (T20, `core/debug_bundle.py`, `ui/debug_report.py`):** Help > Send debug report → zip Sam saves (flash drives
  listed first, "Copy to flash drive" after) with logs, system info, settings (secret keys dropped, values redacted), and for the
  chosen take capture.json + the `core.diagnostics` stage report. Audio only as ticked ~6 s clips around clipping / dropouts /
  lost sound (none ticked by default; D06: nothing is uploaded). `python -m core.diagnostics bundle.zip` prints the same report.
- **Installer (T21, `build/`, `core/mic_check.py`, `core/selftest.py`):** `build\build.ps1` → PyInstaller one-folder
  (`build/hush_studio.spec`, own `.work\build-venv`) → `check_bundle.py` → Inno Setup (`build/hush_studio.iss`) →
  `dist\Hush-Studio-Setup-<APP_VERSION>.exe`: per-user (`%LOCALAPPDATA%\Programs\Hush Studio`, no admin), Start menu +
  desktop shortcut, fixed AppId (upgrade in place), AppMutex `HushStudioAppMutex` (held by main.py). Installed, the app
  is `sys.frozen`: `core.config.bundle_root()` = `_internal` (config, font, `tools/ffmpeg`, `tools/deepfilter`) and
  `DATA_ROOT` = `%LOCALAPPDATA%\HushStudio` (Takes, Intros, cache, data/, logs; settings stay in HKCU), never listed in
  the .iss, so upgrade and uninstall keep them. `HUSH_DATA_DIR` still wins; run from source nothing changes.
  Measured: `deep-filter.exe` imports `VCRUNTIME140.dll`, absent on a clean Windows 10 → the spec ships it beside the
  exe and `check_bundle.py` fails any helper exe that needs a VC/MinGW runtime not beside it (gyan FFmpeg needs only
  OS DLLs). `HushStudio.exe --self-test <report.json>` runs headless: FFmpeg/libx264, ffprobe, deep-filter, libsndfile,
  PortAudio, font, TLS/google-auth, and import → denoise → auto-edit → master → 1 h exact render of a synthetic 60 s
  take (≈ 15 s from source). First run: `mic_check` reads the three Windows privacy switches (HKLM/HKCU ConsentStore
  `microphone`, `\NonPackaged`) and listens 0.5 s (exact zeros = blocked/muted); plain steps + "Open Windows settings"
  (`ms-settings:privacy-microphone`), repeated at start-up until the mic has worked once (`setup/mic_ok`).
  Licences: `build/THIRD-PARTY-NOTICES.md` (PyQt6 + gyan FFmpeg are GPL: fine to give away, not to sell closed).
  `build\verify_install.ps1` automates install → self-test → upgrade → uninstall with data-survival checks on a VM.
- **Takes library (T10, D04, `core/takes.py`, `ui/takes.py`):** one folder per take in `Takes/` (was `sessions/`; renamed once at
  startup, nothing deleted), each with `take.json` (name, created, notes, uploaded). Folder name stays the timestamp; rename only edits
  `name`. Status derived from files: raw → edited (edits.json) → rendered (output_*h.mp4) → uploaded (take.json). Takes screen (header
  "Takes"): search, rename, duplicate (no rendered video), delete (confirmed), Show in Explorer, open in editor / render, export/import
  `.hushtake` zip. Older folders without take.json are listed too.
- **Waveform editor (T12, D01, `core/editing.py`, `ui/editor.py`):** Audacity-style editing of the *edited* take (what
  renders): zoom/scroll/overview, play from any point, select, cut/copy/paste/delete, silence, insert silence, fades,
  markers, undo/redo, plus Auto-edit pauses / Remove plane noise / Normalise on the selection or the whole take, and an
  Open in Audacity → Bring back round-trip (`for_audacity.wav` out; the file brought back is saved as `audacity_N.wav`
  and `EditList.source` names it, edits start fresh, undoable). Ops take output time and become source-time entries:
  `Cut(reason="manual", xfade_s=0.01)`, `Pause(kind="silence", locked)`, `Insert(at, clips)`, `Gain(db/fade, on=insert)`,
  `Marker`. Old edits.json files load unchanged. `EditList.timeline()` is now the one sample-exact description of the
  output (render, waveform, playback, output↔source mapping): matches the previous renderer to 1.5e-8 and renders
  37 min in 0.4 s (was 1.6 s). Behaviour change: a pause partly covered by a cut is clipped and loses that much output
  length (before: dropped, the rest of the gap played raw). Flub proposals show as amber regions (Accept / Undo), applied
  cuts as ▼ at the join (Listen / Restore). Normalise: selection → loudness of the whole take (≤ ±12 dB, peak ≤ −1 dBFS);
  whole take → −21 LUFS (what mastering does anyway, so the waveform shows the real level). Review → "Edit by hand…";
  Takes → "Open in editor" opens it directly. Measured on her real raw take (33 min, 371 pauses, 91 markers): editor
  opens in 0.4 s, a hand edit incl. redraw 21–43 ms, undo ~25 ms, whole-take redraw 15 ms (Qt `fillPath` was 130 ms →
  one line per pixel); Auto-edit pauses 3–4 s, plane noise 4 s per 5 min selection / 17 s whole take, Normalise ~4 s
  (worker thread). raw.flac / clean.wav byte-identical after a full session. Not done: `session.finish(clean=None)` still
  reads clean.wav (the UI always passes the audio; session.py is in T3's zone).
  **Layout = Audacity's, in her colours** (2026-11-10: the first button-row version "doesn't feel enough like Audacity"
  for Sam): menu bar File/Edit/Select/View/Transport/Generate/Effect, icon transport + edit/zoom toolbars and a
  playback meter, track panel (Mute/Select, Waveform/Waveform (dB)), amplitude ruler (click = vertical zoom), clip bar,
  peak + RMS waveform, label track for markers, timeline Quick-Play, selection toolbar with typed hh mm ss.sss boxes,
  Audacity's shortcuts (Space, P, Shift+Space, Home/End, arrows, Ctrl+X/C/V/K/T/L/B/M/Z/Y/1/2/3/E/F/A). Icons are drawn
  in code (not Audacity's art). T14 extends the round trip with an Audacity tab-separated label sidecar for cuts, pauses,
  markers and the playhead (`for_audacity.txt`), passes both files when launching Audacity, and exposes Open in Audacity
  and Import changes from Takes, Review, and the editor. Import remains undoable and stores the returned audio as a new
  `audacity_N.wav` source, leaving the original recording untouched.
- **Live capture (`core/capture.py`):** callback only enqueues; full queue drops + counts (a
  simulated faster-than-real-time source uses backpressure instead). USB mic verified at 48 kHz.
