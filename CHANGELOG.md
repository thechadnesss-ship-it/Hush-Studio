# Changelog

What changed in each Hush Studio release, newest first. The release workflow copies the section
for the tagged version into the GitHub Release notes, so write it for Sam, in plain words.
Format: `## [MAJOR.MINOR.PATCH] - YYYY-MM-DD` (see RELEASING.md).

## [0.3.0] - 2026-11-11

- **Import now shows your audio untouched first.** After you import a recording you see the raw
  take straight away, then choose what to do with a clear list of steps: Remove background noise,
  Auto-edit pauses, Remove plane noise, Normalise and Polish. Each one has a plain description,
  can be previewed and can be undone on its own, and "Do all" gives the usual full result.
- **Check your microphone before recording.** A short calibration listens to the room, then you
  read a sentence at your normal volume. Hush tells you in plain words if the mic is too quiet,
  too loud, or the wrong one (laptop mic, Krisp), and warns you during recording if the level drops.
- **Made for your microphone.** Hush now recognises the Samson Q2U, Rode NT1 and Fifine, and
  opens the Q2U directly so Windows does not change the sound. If another program is using the
  mic it falls back automatically, so recording always works.
- **Fixed: import failing in the installed app**, the intro video missing from the finished
  video, a stray beep, and distortion or cut-off ends of phrases on quiet recordings.
- **Thumbnails with Canva.** An "Open in Canva" button, and Hush picks up the newest picture you
  export so it can be used as the thumbnail.
- **Open in Audacity keeps your markers**, and takes can be imported and exported in more formats.
- **Updates install themselves.** Help > Check for updates shows what is new, with an
  "Update now" button that downloads, checks and installs the new version.
- **Installer.** Hush Studio now comes as one `Hush-Studio-Setup-<version>.exe`: double-click, no
  admin needed, no Python or FFmpeg to install. Upgrading keeps all takes, intros and settings.
- **First-run microphone check.** If Windows is blocking the microphone, the app says which
  switch to turn on and opens the right Windows settings page.
- **Intro manager.** Upload a video or a picture as the intro, name it, preview it, switch
  between intros or choose "No intro". Older intros are kept.
- **Releases are automatic.** Each new version is built, tested and published with its SHA-256
  checksum by GitHub Actions; nobody builds it by hand.

## [0.2] - 2026-11-11

- Waveform editor in the Audacity layout (cut, copy, paste, silence, fades, markers, undo), with
  Auto-edit pauses, Remove plane noise and Normalise as buttons, and an Open in Audacity round trip.
- Takes library: every take saved in its own folder; rename, duplicate, delete, export and import.
- Review screen: proposed flub cuts to accept or undo before rendering; Edit button after recording.
- Fixed the distorted audio and the short audio cuts (dropouts) in recorded takes; a "Recording
  check" line now reports lost sound and clipping for each live take.
- Automated audio quality checks (clipping, distortion, dropouts) at every stage.
- Always-on logging, a friendly crash message, and Help > Send debug report.
