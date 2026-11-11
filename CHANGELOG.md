# Changelog

What changed in each Hush Studio release, newest first. The release workflow copies the section
for the tagged version into the GitHub Release notes, so write it for Sam, in plain words.
Format: `## [MAJOR.MINOR.PATCH] - YYYY-MM-DD` (see RELEASING.md).

## [0.3.0] - 2026-11-11

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
