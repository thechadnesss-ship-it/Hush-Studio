# Releasing Hush Studio

A release is one tag. GitHub Actions (`.github/workflows/release.yml`) then runs the tests,
builds the installer (`build\build.ps1`, including the frozen self-test) and publishes a
GitHub Release with `Hush-Studio-Setup-<version>.exe`, its `.sha256` file and the changelog
notes. Nobody builds anything by hand.

## Every release

1. **Bump the version** in `core/app_info.py`: `APP_VERSION = "0.3.1"` (plain `MAJOR.MINOR.PATCH`).
   It is the only place the version lives.
2. **Add a changelog section** at the top of `CHANGELOG.md`, written for Sam:
   `## [0.3.1] - 2026-11-20` and a few bullets. These become the release notes.
3. Commit on `main` (via the usual task branch → review → merge), then **tag and push**:

   ```powershell
   git tag v0.3.1
   git push origin main v0.3.1
   ```

The workflow refuses a tag that doesn't match `APP_VERSION` and a version with no changelog
section, so a forgotten bump fails in the first minute instead of shipping. Progress and the
SHA-256 are on the repo's **Actions** tab; the installer is also attached to the run as an
artifact for 14 days.

**Pre-release** (to try a build before Sam gets it): tag `v0.3.1-rc.1` (`-rc.2`, …). Same build,
marked *pre-release*, not "latest". Then tag `v0.3.1` for the real release.

**Dry run** without publishing: Actions → Release → *Run workflow*. It runs the tests and builds
the installer as a run artifact.

**A run failed after the build** (e.g. a network blip while publishing): *Re-run failed jobs*.
Publishing an existing tag again replaces its files and notes.

## Where releases go (decision D05)

Releases are published in the separate **public** repo `Hush-Studio-Releases` (builds only), so
Sam and the updater can download without a GitHub account or token; the source repo can be
private. Set the Actions variable `RELEASES_REPO` (`owner/name`) to publish somewhere else.

## One-time setup

1. The repo `<owner>/Hush-Studio-Releases` exists, is public and has at least one commit (a
   README), because a release needs a branch to put its tag on.
2. Create a **fine-grained personal access token**: GitHub → Settings → Developer settings →
   Fine-grained tokens → Generate. Repository access: *Only select repositories* →
   `Hush-Studio-Releases`. Permissions: **Contents: Read and write** (nothing else). Expiry: up
   to a year; put a reminder in the calendar to renew it.
3. In the **source** repo: Settings → Secrets and variables → Actions → New repository secret,
   name `RELEASES_TOKEN`, value = the token.
4. Pushing workflow files needs the `workflow` scope on your git login. If `git push` says
   "refusing to allow an OAuth App to create or update workflow", run `gh auth refresh -s workflow`.

If the secret is missing, the workflow still builds and tests, attaches the installer to the
run, and then fails the publish step with a message pointing here.

## What the build uses

* Windows runner (`windows-2022`), python.org Python 3.12, `requirements.txt` + `build\requirements-build.txt`.
* FFmpeg **9.0.2** essentials (gyan.dev, via GitHub `GyanD/codexffmpeg`) and deep-filter
  **0.5.6**, downloaded at pinned versions and checked against SHA-256 hashes in the workflow.
  To update one, change its version and hash together.
* Inno Setup 6 (preinstalled on the runner; installed with Chocolatey if not).
* Tests on the runner: the quick suite (`pytest -m "not slow"`), plus the frozen self-test inside
  `build.ps1` (FFmpeg, deep-filter, a synthetic take → 1 h video). The full suite, with its
  timing checks for the target laptop, runs before every merge to `main`.
* Sam's own companion GIFs are not in git, so released builds show the drawn moon. That also
  keeps the Pokémon artwork out of the public downloads (CLAUDE.md §6).
* The installer is unsigned: SmartScreen → **More info → Run anyway** (see `build\README.md`).
