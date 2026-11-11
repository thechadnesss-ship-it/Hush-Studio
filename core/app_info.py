"""App identity in one place: change the name here and everything user-facing follows."""

APP_NAME = "Hush Studio"
# The one version number (T22). Release = bump this + a CHANGELOG.md section, then push tag
# v<APP_VERSION> (or v<APP_VERSION>-rc.N for a pre-release); see RELEASING.md. Keep it plain
# numbers (MAJOR.MINOR.PATCH): Inno Setup's VersionInfoVersion only accepts digits and dots.
APP_VERSION = "0.3.0"
SETTINGS_ORG = "HushStudio"          # QSettings location (Windows registry)
USER_AGENT = f"{APP_NAME.replace(' ', '')}/{APP_VERSION} (personal sleep-recording tool)"
