# CLAUDE.md — Hush Studio (formerly PikaMeditate Studio; Design Doc v2: "Encode Once, Copy Forever")

> **Two-agent workflow (Claude Code + Codex):** this file is the architectural source of truth;
> the task plan (`Docs/Engine Build Plan/engine_plan.json`) is managed by **Narthen Control**
> (repo `Narthen-Control`) and holds tasks, priorities, tiers, decisions and the usage budget. Each agent works on its
> own task branch in its own worktree (`../hush-studio-claude`, `../hush-studio-codex`). `AGENTS.md` has the
> protocol and the plan-tool commands; read it at the start of every session. (`ops/` is the old control app, being retired.)

> **For Claude Code:** This is the architectural baseline. The #1 product requirement is
> **render speed**. A session is a **recording of variable length (typically ~30 min)** that gets
> **looped to a 3–8 hour video**, opened by the **same pre-recorded intro** every time, over a black screen.
> Pressing **FINISH** must produce an upload-ready 8 h MP4 in **≈ 15–20 seconds** on a normal laptop.
>
> The core rule: **encode each unique piece of media exactly once, then build the long video
> by stream-copying (`-c copy`) those pieces.** Nothing gets encoded 16 times because it repeats 16 times.
> Any change that re-encodes the full-length output needs explicit sign-off.

---

## 0. What changed from v1 (and why)

| v1 approach | Problem | v2 approach |
|---|---|---|
| `ffmpeg -loop 1 -i black.png ... -c:v libx264` | That **re-encodes every video frame**: ~864,000 frames for 8 h @ 30 fps. Minutes, not seconds. "Without re-encoding" wasn't true. | Pre-encode a **black video loop once** (cached asset). Render time only ever **stream-copies** it. |
| Recording treated as one long 3–8 h take | Real workflow is a ~30 min take, repeated. | **Loop the 30 min take N times** with `-c copy`. Only 30 min of audio is ever processed. |
| Intro not modeled | The same intro gets re-processed every time. | Intro is **mastered + encoded once** and cached; it's reused byte-for-byte. |
| `DeepFilterNet.enhance_frame()` | No such function in the `deepfilternet` Python package; its `df.enhance` API works offline on whole files. | Streaming runtime for the live monitor; offline `df.enhance` on the 30 min take is fine (see Module B). |
| 192 kbps AAC | YouTube re-encodes anyway, and 8 h at 192k ≈ 690 MB to upload. | **128 kbps mono AAC** by default (8 h ≈ 460 MB). Upload is now the slowest step, so smaller wins. |

---

## 0.5 Channel ground truth (measured 2026-11-09): this overrides guesses elsewhere

Output is for the **Sam Katz Hypnosis** YouTube channel. Measured from recent uploads and from
a raw Riverside take vs. Sam's hand-edited mp3 of the same session:

| Topic | Reality | Consequence for the app |
|---|---|---|
| Lengths | 1 h / 3 h / 8 h, to the second (3602 s, 10799 s, 28800 s) | Offer **1 / 3 / 4 / 6 / 8 h**; default fit policy **`exact`** |
| Loudness | Edited takes ≈ **−21 LUFS**, peak ≈ −4 dBFS; published 1 h ≈ −23 LUFS | Master to **−21 LUFS**, not −14 (−14 would be ~7 dB louder than the back catalogue) |
| Intro | A **silent video card** (16–30 s, 1080p30), e.g. disclaimer → visual → "fade to black", then black. **Voice starts at ~0.5 s, under the card.** No intro audio. | Intro is a *video* segment at the start of the video track; audio starts at t = 0 |
| Video | 1080p; black after the intro | Black loop must share the intro's H.264 params (see Module D) |
| Edit (raw → edited) | Waveform almost untouched (corr 0.99, ≈ +1 dB). **No** denoise, EQ, or compression. Flubbed section cut (~2.5 min). Pauses **lengthened** (many to 8–13 s), long pauses **capped ≈ 13 s**, pauses made **digital silence**. ~74% of the edited take is silence. | Auto-edit = **pause shaping + silence + proposed cuts**, all reviewable by Sam. The mastering chain stays light (gain to target + limiter); the HPF/compressor is optional, off by default |
| Raw source | Riverside export: 44.1 kHz mono, pauses already gated (≈ −95 dBFS) | Resample to 48 kHz on import |
| Metadata | Title `<hook>… 😴 \| Deep Sleep Hypnosis (Black Screen)`; category Music; fixed ~18 tags; description = samkatz.store links; daily ~02:00 UTC | `youtube_template.toml` defaults |

**Scope:** primary focus is **new recordings and sessions**. Sam sometimes reuses or rearranges old
audio into a new session; a "repurpose" feature is deferred.

**Editing requirement:** the app does the editing automatically, but Sam must be able to review
and adjust it (move or undo cuts, change pause lengths) before render.

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
  digital silence (her edit: 71%). Not automated: her occasional shorter induction-phase marker
  pauses, and flub cuts (Sam does those in review).
- **FINISH speed on her real take (37:30 edited → exact 8 h, 4-core i5-6500): ≈ 15 s.** Parallel AAC
  encode split at digital-silence pauses (5.5 s vs 11.3 s), and an encode-free `exact` tail: the
  session's first frames up to a ≥ 1 s pause, padded with cached silence frames, so the video ends
  in a sentence break at exactly the target length. Import-only costs (live REC does these during
  recording): noise removal ≈ 20 s and the ear ≈ 14 s for 33 min.
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
- **Live capture (`core/capture.py`):** callback only enqueues; full queue drops + counts (a
  simulated faster-than-real-time source uses backpressure instead). USB mic verified at 48 kHz.

## 1. System Overview

```text
                       ┌──────────────── once, cached ─────────────────┐
  intro.wav ──► master ──► intro.m4a          black loop ──► black_loop_1080p.mp4
                                                                        │
  REC (~30 min) ──► denoise ──► master ──► session.m4a                  │
                                  (once per session, ~seconds)          │
                                                                        ▼
  FINISH ──► concat demuxer, -c copy:   video = black loop × M
                                        audio = intro + session × N   ──► output.mp4 (8 h)
                                                                        │
                                                                        ▼
                                                              YouTube resumable upload
```

### Performance budget (30 min take → 8 h video, mid-range laptop, SSD)

| Stage | When | Budget |
|---|---|---|
| Intro master + encode | First run / when intro changes | ~seconds, then cached |
| Black loop asset | First run / when resolution changes | ~seconds, then cached |
| Denoise 30 min take | FINISH (or live, see Module B) | < 60 s offline · 0 s if done live |
| Master + LUFS + AAC encode of 30 min | FINISH | < 15 s (two-pass loudness on 30 min is cheap) |
| Concat mux to 8 h (`-c copy`) | FINISH | **< 10 s** (disk-I/O bound, ~460 MB write) |
| Upload | After FINISH | Network bound → see Module E |

**Headline:** with live denoise, FINISH → upload-ready 8 h MP4 in **≈ 15–20 s**, and almost all of
that is just mastering the 30 minutes once.

---

## 2. Tech Stack

```text
Language:     Python 3.10+
GUI:          PyQt6 (QThread workers, signals only — never touch widgets off the GUI thread)
Audio I/O:    sounddevice (PortAudio), numpy, soundfile
AI Denoise:   DeepFilterNet3 (streaming runtime live; df.enhance offline fallback)
DSP:          pedalboard (HighpassFilter, Compressor, Gain, Limiter)
Loudness:     pyloudnorm (BS.1770) on the 30 min take — no streaming meter needed anymore
Encode/Mux:   FFmpeg CLI via subprocess (arg lists, no shell=True), FFmpeg ≥ 6
YouTube:      google-api-python-client, google-auth-oauthlib (resumable upload)
Flight data:  httpx → adsb.lol (primary, no key) · airplanes.live / OpenSky (fallbacks)
Plane store:  sqlite3 (stdlib) for overflight history
VAD:          webrtcvad or Silero VAD (speech/pause detection for clean cut points)
```

Drop `ffmpeg-python`; plain `subprocess` is simpler and easier to debug.

---

## 3. Directory Structure

```text
pika_meditate/
├── CLAUDE.md
├── main.py
├── requirements.txt
├── config/
│   ├── presets.toml            # DSP presets ("Sleep Prep", "Deep Focus", ...)
│   ├── render.toml             # target_hours, loop_fps, audio_bitrate, seam_fade_ms
│   └── youtube_template.toml   # title/description/tags templates
├── assets/
│   ├── intro.wav               # YOUR intro source (committed or user-chosen)
│   └── companion_{idle,rec,render,done}.gif
├── cache/                      # GENERATED, gitignored — safe to delete, rebuilt on demand
│   ├── intro_<hash>.m4a        # keyed by hash(intro.wav + preset + bitrate)
│   └── black_loop_1080p_<fps>.mp4
├── sessions/<timestamp>/
│   ├── raw.flac                # untouched mic capture (backup)
│   ├── clean.wav               # denoised
│   ├── session.m4a             # mastered + encoded ONCE
│   ├── concat_audio.txt / concat_video.txt
│   └── output.mp4
├── core/
│   ├── capture.py              # sounddevice InputStream; callback ONLY enqueues
│   ├── denoise.py              # Denoiser interface + backends
│   ├── master.py               # pedalboard chain + LUFS + seam fades + AAC encode
│   ├── asset_cache.py          # builds/validates intro + black loop, hash-keyed
│   ├── renderer.py             # concat-list builder + stream-copy mux
│   ├── youtube_publisher.py
│   ├── plane_guard/
│   │   ├── sky_radar.py        # polls ADS-B feeds, predicts overflights (early warning)
│   │   ├── ear.py              # audio noise detector (backstop: helicopters, no-transponder planes, traffic)
│   │   ├── guard.py            # combines radar + ear → GREEN / AMBER / RED, drives auto-hold/resume
│   │   └── splicer.py          # cuts held sections at speech pauses, crossfades seams
│   └── quiet_planner/
│       ├── logger.py           # records every overflight pass to sqlite
│       └── forecast.py         # weekday×hour heatmap, "best windows today", runway-direction aware
├── ui/
│   ├── main_window.py
│   └── companion.py
└── tests/
    ├── test_mux_speed.py       # synthetic 30 min AAC → 8 h mux must be < 10 s
    ├── test_loop_math.py       # takes of 3, 7, 30, 52, 600 min × targets 3–8 h × all fit policies
    ├── test_seams.py           # no clicks/gaps at loop boundaries
    └── test_loudness.py        # session + intro both land at target ±0.5 LU
```

---

## 4. Module Specs

### Module A — UI & Companion (`ui/`)

> **Implemented 2026-11-09, overrides the theme below.** Brand = Sam's store (samkatz.store):
> white `#FFFFFF`, black text, **purple `#7A23A1`** accent, lilac surfaces (`#D2BFE1`,
> `#F5EFF9`), **Montserrat** (bundled, SIL OFL, `assets/fonts/`), pill buttons. Layout is
> minimalist and **Riverside-style** (she likes Riverside): plane-light status pill, big thin
> timer, plan line, slim level meter; centred round red record button (becomes stop while
> recording) between Pause and Finish & render; "or import a take" link; settings hidden behind
> a link. Green/amber/red appear only for the plane light and a window border glow. Companion
> uses the user's own `assets/companion_{idle,rec,render,done}.gif` if present (personal use),
> else a small drawn moon. Offscreen screenshots: `pytest tests/test_ui.py` → `.work/ui_*.png`.

- Theme (original v2 spec, superseded): `#1E1E2E` background, `#CBA6F7` accents.
- Header: Preset dropdown · **Target length** (3 / 4 / 6 / 8 h) · Status pill · Companion.
- Center: `HH:MM:SS` timer (of the take) + a small "→ loops ×N = 7h 58m" preview line · VU meter.
- Controls: `[ ⏺ REC ]` `[ ⏸ PAUSE ]` `[ ⏹ FINISH & RENDER ]`.

**Companion states** (`QMovie` per state):

| State | Sprite | Bubble |
|---|---|---|
| IDLE | `companion_idle.gif` | "Ready to record your session." |
| RECORDING | `companion_rec.gif` | "Listening… noise filter active (planes muted)!" |
| PLANE_INCOMING (amber) | `companion_rec.gif` | "Plane in ~{s}s, finish your sentence." |
| PLANE_HOLD (red) | `companion_idle.gif` | "Holding… plane overhead. I'll tell you when it's clear." |
| CLEAR (green flash) | `companion_rec.gif` | "All clear! Carry on whenever you're ready." |
| MASTERING | `companion_render.gif` | "Polishing your 30 minutes…" |
| RENDERING | `companion_render.gif` | "Looping ×{N}… 8 hr video in seconds!" |
| UPLOADING | `companion_render.gif` | "Sending to YouTube… {pct}%" |
| SUCCESS | `companion_done.gif` | "Uploaded! Tap to open." |
| ERROR | `companion_idle.gif` | Short message + "Details" |

### Module B — Capture & Denoise (`core/capture.py`, `core/denoise.py`)

- **48 kHz, mono, float32.** DeepFilterNet's official models run at 48 kHz with 480-sample (10 ms) hops.
- Audio callback only does `queue.put_nowait(indata.copy())`. No inference or file I/O in the callback.
- Always write the **raw** take to `raw.flac` (backup; lets you re-run denoise with other settings).
- `Denoiser` interface: `process(chunk) -> chunk`, `flush() -> tail`. Two backends:
  - **Live (preferred):** a streaming DeepFilterNet runtime, e.g. the third-party `deepfilternet-rs`
    package (PyO3 bindings around the official Rust real-time runtime: `process_chunk()` / `finalize()`).
    Pin and wrap it. Output feeds the monitor *and* `clean.wav`, so denoise costs 0 s at FINISH.
  - **Offline fallback:** `df.enhance()` from the `deepfilternet` package on the whole 30 min file
    at FINISH. Fine for 30 min; adds well under a minute on a decent CPU.
- Expose `attenuation_limit_db` per preset (lower = more room tone / "vocal warmth" kept).

### Module C — Master Once (`core/master.py`)

Runs on `clean.wav` (30 min) at FINISH, and on `intro.wav` when the cache is built. **Both must go
through the same chain and land at the same loudness**, or the intro will jump in volume.

1. pedalboard chain:
   ```python
   Pedalboard([
       HighpassFilter(cutoff_frequency_hz=80),
       Compressor(threshold_db=-16, ratio=2.5, attack_ms=10, release_ms=200),
   ])
   ```
2. Measure integrated loudness (pyloudnorm), apply gain to **-14 LUFS**, then `Limiter(threshold_db=-1.0)`.
   (Two-pass is cheap now: 30 min, not 8 h.)
3. **Seam fades:** fade-in and fade-out of `seam_fade_ms` (default 1500 ms) at the start and end of the
   take. Loop boundaries then go silence→silence, so the loop is inaudible.
4. Encode once: `ffmpeg -i mastered.wav -c:a aac -b:a 128k -ac 1 -ar 48000 session.m4a`.

### Module D — Renderer (`core/asset_cache.py`, `core/renderer.py`)

**Cached black loop** (built once, keyed by resolution + fps):

```bash
ffmpeg -y -f lavfi -i color=c=black:s=1920x1080:r=1 -t 60 \
  -c:v libx264 -preset veryfast -tune stillimage -pix_fmt yuv420p \
  -g 60 cache/black_loop_1080p_1.mp4
```

- 1 fps black H.264 is near-zero bytes and very few packets to mux. `loop_fps` is a config flag;
  if YouTube processing ever misbehaves at 1 fps, rebuild at 24/30 (render time barely changes).

**Loop math: fully dynamic, driven by the take's real length.**

Nothing assumes 30 minutes. `session_dur` is read with `ffprobe` from `session.m4a` after
mastering (sample-accurate), and `intro_dur` from the cached intro. Any take length works:
a 7 min take loops ~68× for 8 h; a 52 min take loops ~9×; a take longer than the target is used once, unlooped.

```text
remaining = target_hours*3600 - intro_dur
if remaining <= session_dur:      N = 1                    # take alone fills (or exceeds) target
else:                             N = loops_for(policy)    # see fit policy below
total = intro_dur + N * session_dur (+ tail_dur if policy == "exact")
M = ceil(total / black_loop_dur)                         # black-loop copies
```

**Fit policy** (`render.toml → fit_policy`, also a dropdown in the UI):

| Policy | Rule | Length accuracy | Extra cost |
|---|---|---|---|
| `nearest` (default) | `N = round(remaining / session_dur)` | within ± half a take | none |
| `at_least` | `N = ceil(remaining / session_dur)` | never shorter than target | none |
| `exact` | `N = floor(remaining / session_dur)`, then add a **tail**: the first `remaining − N·session_dur` seconds of the take, with a fade-out, encoded once as `tail.m4a` | to the second | encode ≤ 1 take of audio (seconds) |

Whole-loop policies always end on the take's natural fade-out. `exact` still never re-encodes the
long output: only the short tail is encoded, and it's concat-copied like everything else.

**Before rendering**, the UI shows the live plan as the take grows, e.g.
`Take 34:12 · intro 1:05 · ×14 → 7h 59m 53s`, and updates if the user changes target or policy.

**Edge cases to handle:**
- Take shorter than `2 × seam_fade_ms` + 10 s → refuse with a friendly message (too short to loop).
- Very short takes (< 2 min) → warn: hundreds of repeats will be noticeable to listeners.
- Take resumed after PAUSE → duration is the sum of recorded audio, not wall-clock time.
- Concat lists with thousands of lines are fine for FFmpeg; no special handling needed.

**FINISH mux: native stitcher (`core/mp4_stitch.py`), zero encoding.** *(Implemented 2026-11-09.)*
Measured on the target laptop, the FFmpeg concat command below took **~42 s** for 8 h: the concat
demuxer handles ~1.35 M AAC frames one by one (~10 s), and the MP4 muxer's index finalisation adds
~23 s. Every segment is the same bytes repeated, so `mp4_stitch.py` instead **byte-copies** each
segment's encoded samples into one `mdat` and writes the `moov` index directly from the source
sample tables (numpy). Result: **8 h in ~0.4–0.8 s** (disk-bound), exact duration, full decode clean.
- Inputs must be single-track, contiguous, no `ctts`: the black loop is therefore built with `-bf 0`.
  Anything else raises `UnsupportedInput` and the renderer falls back to the FFmpeg command below
  (fragmented MP4 via `-movflags +frag_keyframe+empty_moov`, ~10 s).
- Plan durations come from the stitched layout: each segment counts its full packet length (AAC
  priming + padding ≈ 21–43 ms of silence per join, inaudible under the seam fades); only the
  intro's priming is skipped, via the edit list.
- Write in ≤ 4 MB chunks: on Windows a single 28 MB `write()` is ~20× slower.
- AAC encodes use `aac_coder = "fast"` (~2× quicker than `twoloop` on speech/room tone: ~13–16 s
  per 30 min). Don't benchmark it on a pure sine; it's pathologically slow there (~5×).

**Fallback FFmpeg mux (one call, zero encoding):**

```text
concat_audio.txt:  file 'cache/intro_<hash>.m4a'
                   file 'session.m4a'        ← repeated N times
concat_video.txt:  file 'cache/black_loop_1080p_1.mp4'   ← repeated M times
```

```bash
ffmpeg -y -f concat -safe 0 -i concat_video.txt \
          -f concat -safe 0 -i concat_audio.txt \
  -map 0:v:0 -map 1:a:0 -c copy -t <total_seconds> output.mp4
```

- `-c copy` on both streams. Cost ≈ writing the output file.
- Use `-t <total>` instead of `-shortest` (predictable with stream copy).
- **No `+faststart`** on the upload copy; it rewrites the whole file a second time and YouTube doesn't need it.
- Intro and session **must share codec params** (AAC-LC, 48 kHz, mono, same bitrate) or concat copy fails.
  `asset_cache.py` validates this with `ffprobe` and rebuilds the intro if it doesn't match.
- AAC encoder priming adds a few ms of silence at each segment join. The seam fades make that
  inaudible; `test_seams.py` verifies there are no clicks.
- Progress: parse `-progress pipe:1` on a QThread.

### Module E — YouTube Publisher (`core/youtube_publisher.py`)

- OAuth2 installed-app flow; token in `token.json` (OS keychain if available).
- `MediaFileUpload(path, chunksize=64*1024*1024, resumable=True)`; chunk size must be a multiple
  of 256 KB. Exponential backoff on 5xx/connection errors; persist the resumable session URI so
  uploads survive an app restart.
- Metadata from `youtube_template.toml` (`{date}`, `{duration}`, `{preset}` placeholders).
- **Important limitation:** videos uploaded through an *unverified* Google Cloud API project
  (created after 28 July 2020) are **locked to private**, whatever `privacyStatus` you request.
  Either flip them to Public/Unlisted in YouTube Studio, or apply for Google's API compliance
  audit. Show this plainly on first upload.
- Quota: Google moved `videos.insert` to its own daily bucket in mid-2026 (reported default
  100 uploads/day). Not a constraint here, but check the current quota page during setup.
- **Upload is now the slowest step.** Levers: 96–128 kbps mono AAC, 1 fps video (near-zero bytes),
  and "upload in background" so you can start the next session.

---

### Module F — Plane Guard (`core/plane_guard/`)

**Goal:** she can record at any time; the app keeps planes out of the take automatically.

**Location context:** home is De Tyger (Parow), Cape Town, roughly 5–6 km north of Cape Town
International (FACT), near the extended line of main runway 01/19. Runway 01 departures and
runway 19 arrivals pass close by at low altitude, and which one is in use depends on the wind.
Home coordinates are set once in Settings (default ≈ -33.91, 18.61), stored locally only, and
rounded to ~1 km before being sent to any flight API.

**1. Traffic light** (big, always visible beside the timer, plus a full-window border glow):

| Light | Meaning | What she does |
|---|---|---|
| 🟢 GREEN | Sky clear, recording | Speak normally |
| 🟠 AMBER | Plane predicted within `amber_lead_s` (default 45 s) | Finish the sentence, pause |
| 🔴 RED | Plane audible / overhead, take is **on hold** | Wait |
| 🟢 flash + soft chime | Clear for `clear_hold_s` (default 6 s) | Carry on |

Optional: a short soft chime on AMBER and CLEAR so she doesn't need to watch the screen
(chime goes to her headphones only, never into the recording).

**2. Sky radar (`sky_radar.py`), the early warning:**
- Poll `https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/15` every 5 s while recording
  (every 60 s when idle, for the planner). Fallbacks: airplanes.live point endpoint, then OpenSky
  bounding box. Respect each service's rate limits; back off on 429.
- For each aircraft: project position forward using ground speed + track; compute
  **closest-approach distance and time**, and altitude at that point.
- **Loudness score** = f(closest distance, altitude, aircraft category: heavy jets score higher).
  Trigger AMBER when predicted score > threshold and time-to-closest-approach < `amber_lead_s`.
- **FACT-specific trick:** departures off runway 01 are only ~60–90 s from overhead. Watch for
  aircraft **on the ground at FACT, accelerating on runway 01** (ground speed > 40 kt, on the
  runway polygon). That gives AMBER as the takeoff roll starts.
- Arrivals to runway 19 come from the north and are visible several minutes out, which is plenty of warning.
- ADS-B doesn't see everything (some light aircraft, some military, helicopters), so the radar
  **never clears RED on its own**. Only the ear can.

**3. Ear (`ear.py`), the backstop and the final judge:**
- Compares the raw mic signal with DeepFilterNet's output: `noise = raw − clean`. Tracks noise
  energy in 50–2000 Hz (where jet roar lives) on 100 ms frames.
- Calibrated in a 10 s soundcheck at REC (room noise floor + her speaking level).
- RED when noise is within `red_snr_db` (default 12 dB) of her speaking level for > 1 s.
- CLEAR when noise is back within 3 dB of the room floor for `clear_hold_s`, and the radar shows nothing inbound.
- Hysteresis on every threshold, so the light never flickers.

**4. Auto-hold and auto-resume (`guard.py` + `splicer.py`):**
- Capture **never stops** (raw FLAC keeps everything, for safety and re-edits). "Hold" means
  the app marks a **cut region** in the take.
- **Cut start** = the last natural pause in her speech (VAD) up to 8 s *before* RED, so a
  half-spoken sentence under a rising plane is removed cleanly instead of being chopped mid-word.
  If AMBER fired and she stopped speaking, the cut starts at her pause.
- **Cut end** = CLEAR. Any silence between CLEAR and her next word is trimmed to `max_gap_s`
  (default 2.5 s) so the pacing still sounds natural.
- At FINISH, `splicer.py` removes cut regions from `clean.wav` with 300 ms equal-power
  crossfades, *before* mastering. The take timer only counts kept audio.
- **Review list** at FINISH: "3 plane cuts (1m 42s removed)", each with ▶ preview and **Keep anyway**.
- Manual override: the PAUSE button still works, and a "Guard off" toggle exists for quiet days.

### Module G — Quiet-Time Planner (`core/quiet_planner/`)

**Goal:** show her the best times to record before she sits down.

- `logger.py` runs whenever the app is open (60 s polling, ~1 request/minute). It logs each
  **pass** (an aircraft that came within 3 km horizontally and below 1,500 m) to sqlite:
  timestamp, callsign, altitude, closest distance, direction (arrival/departure), loudness score.
- `forecast.py` builds:
  - **Weekday × hour heatmap** of expected loud passes per hour.
  - **"Best windows today"**: the top 3 windows ≥ 45 min with the fewest predicted passes,
    shown on the home screen, e.g. *"Quietest today: 13:10–14:20 (~1 plane), 21:30–23:00 (~0)"*.
  - **Runway-direction awareness:** which runway is in use (inferred from recent traffic) changes
    which planes pass over you. The forecast splits history by runway mode and weights by the
    current one. Optionally use a free wind forecast to predict runway changes later in the day.
- **Cold start:** with no history yet, show a generic airport curve (morning and evening peaks,
  quieter late night) labelled "learning your sky, ~7 days to personalise". Accuracy improves weekly.
- Optional "Remind me" notification 10 min before a quiet window starts.
- Honest framing in the UI: it's a forecast, not a promise. Schedules change, and the guard still runs.

**Data-use note:** OpenSky is free for non-commercial/research use; adsb.lol and airplanes.live
are free community feeds with no uptime guarantee. All fine for a personal tool. If the app is
ever sold, revisit licensing.

## 5. Build Order for Claude Code

1. **`asset_cache.py` + `renderer.py` + `test_mux_speed.py` first.** Use a synthetic 30 min tone
   + fake intro; prove 8 h mux < 10 s and that the duration is exact. This is the headline promise.
2. `master.py` + `test_loudness.py` + `test_seams.py`. Prove intro and session match loudness and loop seamlessly.
3. `capture.py` with raw FLAC + offline denoise → end-to-end REC → FINISH → MP4.
4. Live streaming denoise backend + monitor output (benchmark on target hardware).
5. Plane Guard: ear + splicer first (works offline, testable on recorded plane clips), then sky radar.
6. Quiet-time planner (start the logger early so history accumulates while the rest is built).
7. UI + companion state machine + traffic light.
8. YouTube publisher.

## 6. Conventions & Gotchas

- Never block the GUI thread; all FFmpeg calls and uploads run in QThread workers emitting signals.
- FFmpeg args are always lists; check FFmpeg ≥ 6 at startup.
- `cache/` is disposable; every cached file is keyed by a hash of its inputs and settings.
- **Artwork:** the mascot is Pokémon/Nintendo IP. Fine for a personal tool, but if the app is ever
  shared, sold, or shown on the channel, swap in an original mascot; the `companion_*` names make it a drop-in.
