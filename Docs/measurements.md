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
- **Real-asset render (re-measured 2026-11-11, T6):** Sam's 33:06.66 take + the real intro card → exact **8 h 00 m 00 s**. Import 16.81 s, DeepFilterNet denoise 176.96 s, edit preparation 25.59 s; FINISH itself: apply edits 0.08 s, master/AAC encode 12.43 s, asset validation/build 12.22 s, native stream-copy render 2.94 s (**27.59 s FINISH**, **247.07 s end-to-end** including import/denoise/prepare). Output was 159,947,884 bytes and measured 28,800.0 s; 13 session loops plus a 998.997 s encode-free exact tail. (The prior ≈5 s figure was a cached/synthetic-style render measurement; this is the current real-take measurement.)
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
- **Laptop test fixes (T24, 2026-11-11, Sam's debug bundle):** (1) Import of wav/mp3 failed in the installed app:
  it is console-less (`console=False`), and an FFmpeg child started with `capture_output` but no stdin redirect
  inherits the invalid stdin handle → `OSError: [WinError 6] The handle is invalid` (reproduced in
  `tests/test_t24_laptop_fixes.py` with `SetStdHandle(-10, bogus)`). Rule: **every** subprocess gets
  `stdin=DEVNULL` (or PIPE) + `ff._POPEN_FLAGS` (CREATE_NO_WINDOW); `ff.run` does it for all its callers.
  `load_mono_48k` raises "FFmpeg could not read this audio file: …"; worker errors are logged with the traceback.
  (2) No intro in the video: the installer never shipped a card, so a clean install seeded "Intro: none". Now
  `assets/intro_card.mp4` (1920×1080, 30 fps, 30 s; git-tracked via a `.gitignore` exception) is bundled,
  required by `check_bundle.py`, and the self-test renders with it and checks the first seconds aren't black
  (mean grey ≈ 12.6 at 5 s vs 0.0 in the black loop). The self-test also imports wav/mp3/m4a/flac. (3) A soft beep
  in her take: the plane-guard chime played through laptop speakers into the mic → off unless QSettings
  `chime_on=true` (headphones only).
- **Releases (T22, D05, `.github/workflows/release.yml`, `RELEASING.md`):** the one version is `core/app_info.py`
  `APP_VERSION` (plain `MAJOR.MINOR.PATCH`: Inno's VersionInfoVersion rejects suffixes). Push tag `v<APP_VERSION>` (release)
  or `v<APP_VERSION>-rc.N` (pre-release) → `windows-2022` runner: `.github/scripts/release_info.py` checks tag = version and
  takes the `CHANGELOG.md` `## [<version>]` section as notes → FFmpeg 9.0.2 essentials (GyanD/codexffmpeg) + deep-filter
  0.5.6 downloaded and SHA-256 checked → quick suite (`-m "not slow"`, `PIKA_OFFLINE=1`) → `build\build.ps1` (frozen
  self-test) → `.sha256` (sha256sum format) → run artifact (14 days) → `gh release create` in the public
  `Hush-Studio-Releases` repo with secret `RELEASES_TOKEN` (fine-grained, Contents R/W on that repo only; variable
  `RELEASES_REPO` overrides). Re-running a tag replaces the files. Companion GIFs aren't in git → released builds show the moon.
  Measured (run for `v0.3.0-rc.2`, 2026-11-11): tag → artifact in ≈ 4.5 min; Setup.exe 127.7 MB, checksum OK; on this PC it
  installs silently in 32 s and `--self-test` passes every check in 54 s (1 h exact render incl.). Publishing waits on Sam
  adding the `RELEASES_TOKEN` secret (RELEASING.md, one-time setup); without it the last step fails with that message.
- **Updater (T23, D05, `core/updater.py`, `ui/updates.py`):** 3 s after start (skipped when `PIKA_OFFLINE=1`) and from
  Help → Check for updates…, `GET api.github.com/repos/thechadnesss-ship-it/Hush-Studio-Releases/releases/latest` (no token;
  rc pre-releases are never "latest"; 404 = no releases yet). Newer → What's new (CHANGELOG section, build footer stripped),
  Update now / Skip this version / Later, plus a header link "Update to X". Auto-check is silent offline, when up to date, on
  a skipped version, and never pops over recording/render/review/upload or another dialog (`MainWindow.update_blocker()`;
  re-checked after the download). Download to `DATA_ROOT\updates`, SHA-256 from the `.sha256` asset (mismatch → deleted).
  Hand-off: a PowerShell script waits for the app's PID (+ name) to exit **and** `HushStudioAppMutex` to be free (with
  `/SUPPRESSMSGBOXES` Inno would silently cancel on its "app is running" prompt), runs Setup `/VERYSILENT /SUPPRESSMSGBOXES
  /NORESTART /NOCANCEL /SP-`, writes `install.exitcode` and reopens `sys.executable`. Measured: `DETACHED_PROCESS` makes
  powershell.exe exit without running the script; use `CREATE_NO_WINDOW`. Next start: "Updated to X" note + downloads removed,
  or "didn't finish" + Setup log path in the app log. From source, Update now opens the release page. Test overrides:
  `HUSH_UPDATE_API`, `HUSH_RELEASES_REPO`. E2E 2026-11-11 on the dev PC: real 0.3.0 install → fake endpoint serving a 0.3.1
  build → Update now: download + verify 194 MB ≈ 6 s (localhost), app closed, Setup ≈ 40 s, reopened on 0.3.1 ≈ 44 s after
  the click, takes intact. Sam's first in-app upgrade is from the first published release that contains the updater.
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
- **Import → Steps panel (T26, `ui/steps.py`, `ui/editor.py`, `core/editing.py`):** an import runs nothing but the
  import (`ImportWorker`): one ffmpeg pass writes raw.flac (FLAC level 0, 24-bit) and returns the samples; 33 min
  44.1 kHz wav → **2.7 s** (was 16.5 s with soundfile's FLAC writer: 7 s of it), then the editor opens on the raw take
  (`EditList.source = "raw.flac"`, empty list). Steps panel on the editor's right: Remove background noise, Remove
  plane noise, Auto-edit pauses, Normalise, Studio polish, each Apply / Undo / ▶ Listen (20 s from the cursor), plus
  **Do all** = noise → planes → auto-edit → normalise (polish stays her choice, off by default). Each step is one
  undoable change and can be taken back on its own, out of order: noise removal swaps the audio under the edits
  (`Editor.set_source`, raw.flac ↔ clean.wav, sample-aligned, so cuts/pauses stay put); auto-edit = pauses + flub
  proposals (`compute_auto_edit`, identical to `session.prepare`, incl. dropping a flub that overlaps an earlier one);
  polish is `EditList.polish` (render reads it). Do all gives exactly the old `prepare()` cuts and pauses (tested
  sample-for-sample) plus a whole-take Normalise gain, which mastering then leaves at ≈ 0 dB. `finish(clean=None)` now
  reads `edl.source`. Live takes keep the automatic path (prepare → Review); in the editor the same panel shows those
  steps applied, each undoable. Not done: imports skip the Review dialog, so its source-vs-clean quality check no
  longer runs for imports.
- **Live capture (`core/capture.py`):** callback only enqueues; full queue drops + counts (a
  simulated faster-than-real-time source uses backpressure instead). USB mic verified at 48 kHz.
  Mic profiles (T29, `core/mic_profiles.py`, `config/mics.toml`, `core/mic_select.py`): Samson Q2U (prio 1, Sam's),
  Rode NT1 (2), Fifine (3), else Generic. Names are matched on every host API, deduped (MME's 31-char names fold in);
  a strong match (own name: 'q2u', 'fifine', 'rode'/'nt-usb'/'nt1') beats a weak one ('samson'; 'USB PnP Sound Device'
  = Fifine; Focusrite/Scarlett/UMC/... interface = the XLR NT1), then lowest priority wins; `override` (profile id) wins
  over all; unknown inputs are logged once. Auto open: the detected mic's best host API (WASAPI exclusive → shared →
  WDM-KS → DirectSound → MME) → system default; Generic = system default as before. `capture.json` gets `mic_profile`.
  `python -m core.mic_select [--profile id]` prints inputs, the detection and the open order. Dev PC 2026-11-11 with the
  Q2U: detected `samson_q2u`, open order WASAPI #21 excl/shared → WDM-KS #33 → DS #9 → MME #1.
  Per-mic path + level check (T30): the stream is always 48 kHz mono float32 (the pipeline's rate; no per-mic rate);
  each profile sets `blocksize` (4800 = 100 ms, the meters assume it), `latency` ("high": bigger driver buffer, no
  monitor to keep in sync) and `auto_convert` (WASAPI *shared* opens with Windows' converter, so a mic Windows runs
  at 44.1 kHz still opens at 48 kHz). Exclusive fails while Riverside/Zoom/Teams hold the mic → shared → WDM-KS →
  DS → MME → default; the log says `input opened: <dev> [<api>, <mode>] ... (fell back from: ...)` and
  `input closed: ..., N input overflow(s)`; `capture.json` gets `mode`, `blocksize`, `fell_back_from`.
  `core/mic_check.check_level()` / `python -m core.mic_check --seconds 30 [--profile id]` records through the same
  MicSource path and judges the mic's own level (before calibration gain): peak < −22 dBFS = quiet (> 12 dB boost),
  clipped or peak ≥ −2 = too loud, else good; voice < 15 dB over the room adds the mic's room tip; any overflow =
  not ok. Tips are per profile (`quiet_tip`/`loud_tip`/`room_tip`, built-ins in `mic_profiles.py`): Q2U → Windows
  level to 100 and 5–10 cm; NT1 → interface gain knob down, ~20 cm, it hears the whole room; Fifine → Windows
  level 100 + gain knob. Self-test runs the three verdicts on every profile. Dev PC 2026-11-11 with the Q2U:
  opened WASAPI exclusive first try, stream latency 0.11 s, 0 overflows, room −65.8 dBFS (no speech → "couldn't
  hear you"). The 30 s speech runs with all three mics (NT1, Fifine not here) are Sam's manual Verify.
- **Mic calibration (T27, `core/calibration.py`, `ui/calibrate.py`):** Settings → "Check mic" (offered once on first run
  after the T21 mic check passes, and at REC for a mic not yet checked: "Check mic" / "Record anyway", asked once per mic
  per run). Setup checks by device name (WASAPI list + defaults): laptop array / Intel SST vs USB mic (laptop default with
  a USB mic plugged in = "wrong mic"), Krisp / NVIDIA Broadcast / Steam / VB-Cable inputs, Bluetooth hands-free, 48 kHz
  (`check_input_settings`), playback on speakers vs headphones (headphones in a USB mic's jack appear as
  "Speakers (<mic>)" and count as headphones), Windows privacy switches (no Start while blocked, or with no mic).
  Then 5 s quiet → floor = median 100 ms RMS; a sentence → sample peak + RMS of blocks ≥ floor + 10 dB.
  Gain = −10 dBFS − sentence peak (0.5 dB steps, ±1 dB dead band, −18…+30 dB): one sentence peaks a few dB under a whole
  take, so a take lands in **−12…−6 dBFS** (test: Sam's −29.5 dBFS Q2U level → +19.5 dB → a 90 s take with a +2.5 dB
  moment peaks inside the zone in raw.flac). Advice: clipping (full-scale samples or flat tops → no save), far too quiet
  (> +30 dB needed), quiet (> +12 dB: boosted, but turn the mic up / move closer), a little loud (turned down), floor
  ≤ −90 dBFS = a gate/suppressor in the chain, voice < 15 dB over the room = noisy. Stored per device as JSON in
  QSettings `calibration/<name, lower-case, first 31 chars>` (HKCU; MME truncates names to 31). The Recorder multiplies
  every block by the gain **before raw.flac** (so raw/diagnostics show the real recorded level; `capture.json`
  `input_gain_db` keeps the factor) and still counts clipping at the mic's own level when the gain is negative.
  `LevelWatch` (last 20 s of voice, needs 5 s): peaks < −20 dBFS → "quiet", > −2 → "loud", with hysteresis, shown in the
  red mic line under the meter (clipping / lost sound keep priority). The meter draws the target zone once the mic is
  checked. Dev PC has no input device (only Realtek WDM-KS + Steam virtual), so the real USB / laptop mic run is Sam's.
