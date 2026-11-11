"""Mic profiles + autodetect (T29): Samson Q2U, Rode NT1, Fifine, else Generic.

Profiles live in `config/mics.toml` (built-in copies below are used if that file is missing or
broken, e.g. in an old installer). `detect_mic()` reads every input device on every host API,
dedupes the names (one USB mic appears 3-4 times on Windows), matches them against the profiles and
returns the best one:

* a *strong* match (the mic's own name, e.g. 'Samson Q2U Microphone', 'FIFINE K678', 'RODE NT-USB')
  beats a *weak* one (a generic chip or interface name, e.g. 'USB PnP Sound Device', 'Focusrite');
* among equals the lowest `priority` wins (Q2U 1, Rode NT1 2, Fifine 3; see mics.toml);
* `override` (profile id or name; None/'auto' = detect) always wins: that is the UI's choice;
* nothing matched, or sounddevice is unavailable = Generic. Detection never raises.

Unmatched device names are logged once each so new mics can be added to mics.toml.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .log_setup import get_logger

log = get_logger("capture")

GENERIC_ID = "generic"
DEFAULT_MICS_TOML = Path(__file__).resolve().parent.parent / "config" / "mics.toml"

# Names Windows lists as inputs that are never a real mic; skipped quietly (not "unmatched").
_NOT_A_MIC = re.compile(r"sound mapper|primary sound capture|stereo (mix|input)|what u hear|wave out mix|"
                        r"loopback|steam streaming|vb-audio|cable output|voicemeeter", re.I)


@dataclass(frozen=True)
class MicProfile:
    id: str
    name: str
    priority: int = 99
    patterns: tuple[str, ...] = ()
    weak_patterns: tuple[str, ...] = ()
    type: str = "unknown"                # dynamic | condenser | unknown
    connection: str = "unknown"
    expected_speech_dbfs: float | None = None
    best_host_api: str = ""              # e.g. "Windows WASAPI"; "" = no preference
    exclusive: bool = False              # try WASAPI exclusive first
    notes: str = ""
    # T30: how to open it. The stream is always 48 kHz mono float32 (the whole pipeline's rate);
    # `auto_convert` lets WASAPI *shared* mode use Windows' converter when the mic runs at another rate.
    blocksize: int = 4800                # frames per callback (100 ms; the live meters assume 100 ms)
    latency: str | float = "high"        # PortAudio latency hint: "high" = bigger driver buffer = no lost sound
    auto_convert: bool = True
    # T30: plain-language tips for the level check (core/mic_check.check_level)
    quiet_tip: str = ""
    loud_tip: str = ""
    room_tip: str = ""

    @property
    def is_generic(self) -> bool:
        return self.id == GENERIC_ID

    def match(self, device_name: str) -> int:
        """2 = strong (its own name), 1 = weak (generic chip / interface), 0 = no match."""
        if any(re.search(p, device_name, re.I) for p in self.patterns):
            return 2
        if any(re.search(p, device_name, re.I) for p in self.weak_patterns):
            return 1
        return 0


_SOUND = "Windows Settings → System → Sound → your microphone → Volume"

GENERIC = MicProfile(GENERIC_ID, "Generic", notes="Any other mic: Windows' default input, no assumptions.",
                     quiet_tip=f"Turn the mic level up ({_SOUND}) to 80–100 and speak a little closer to the mic.",
                     loud_tip=f"Turn the mic level down ({_SOUND}) a little, or sit a little further back.",
                     room_tip="Move closer to the mic, and close windows or switch off fans if you can.")

_Q2U_TIPS = dict(
    quiet_tip=f"The Samson Q2U is a quiet mic. Turn its Windows level to 100 ({_SOUND}) and speak about a "
              "hand's width (5–10 cm) from the front of the mic.",
    loud_tip=f"Turn the Q2U's Windows level down to about 80 ({_SOUND}), or sit back a little.",
    room_tip="Speak closer to the Q2U, and close windows or switch off fans if you can.")
_NT1_TIPS = dict(
    quiet_tip="Turn the gain knob on your audio interface up a little (for a USB Rode, turn its Windows level "
              f"up: {_SOUND}).",
    loud_tip="The Rode NT1 is very sensitive. Turn the gain knob on your audio interface down until its light "
             f"stays green (for a USB Rode, turn its Windows level down: {_SOUND}), and sit about 20 cm back.",
    room_tip="The Rode NT1 hears the whole room. Close windows and doors, switch off fans, and speak a little "
             "closer to it.")
_FIFINE_TIPS = dict(
    quiet_tip=f"Turn the Fifine's Windows level up to 100 ({_SOUND}). If the mic has a gain knob, turn it up "
              "too, and speak a little closer.",
    loud_tip=f"Turn the gain knob on the Fifine down (or its Windows level: {_SOUND}), or sit back a little.",
    room_tip="The Fifine hears the room. Speak closer to it, and close windows or switch off fans if you can.")

BUILTIN_PROFILES: tuple[MicProfile, ...] = (
    MicProfile("samson_q2u", "Samson Q2U", 1, ("q2u",), ("samson",), "dynamic", "usb", -29.5,
               "Windows WASAPI", True, "Sam's mic.", **_Q2U_TIPS),
    MicProfile("rode_nt1", "Rode NT1", 2, (r"\br(o|ø|\?)de\b", r"\bnt-?1\b", r"nt-?usb"),
               ("scarlett", "focusrite", "audient", r"\bevo\b", "motu", "steinberg", r"\bur(12|22|24)", r"umc\d",
                "behringer", "presonus", "audiobox", r"volt \d", "universal audio", r"ssl \d", "tascam", "xlr"),
               "condenser", "usb or xlr (via interface)", -20.0, "Windows WASAPI", True, "Condenser.", **_NT1_TIPS),
    MicProfile("fifine", "Fifine", 3, ("fifine",), (r"usb pnp (sound|audio) device",), "condenser", "usb", -30.0,
               "Windows WASAPI", True, "Budget USB condenser.", **_FIFINE_TIPS),
    GENERIC,
)


def load_profiles(path: Path | str | None = None) -> list[MicProfile]:
    """Profiles from mics.toml (sorted by priority), always ending with Generic."""
    p = Path(path) if path is not None else DEFAULT_MICS_TOML
    profiles: list[MicProfile] = []
    builtin = {b.id: b for b in BUILTIN_PROFILES}
    try:
        for m in tomllib.loads(p.read_text(encoding="utf-8")).get("mic", []):
            for pat in (*m.get("patterns", []), *m.get("weak_patterns", [])):
                re.compile(pat)                           # a bad regex = broken file, use built-ins
            lvl = m.get("expected_speech_dbfs")
            base = builtin.get(str(m["id"]), GENERIC)            # tips the file leaves out
            block = int(m.get("blocksize", 4800))
            if not 480 <= block <= 9600:
                raise ValueError(f"blocksize {block} for {m['id']} is outside 480..9600")
            lat = m.get("latency", "high")
            if not (lat in ("high", "low") or isinstance(lat, (int, float))):
                raise ValueError(f"latency {lat!r} for {m['id']} must be 'high', 'low' or seconds")
            profiles.append(MicProfile(
                id=str(m["id"]), name=str(m.get("name", m["id"])), priority=int(m.get("priority", 50)),
                patterns=tuple(m.get("patterns", [])), weak_patterns=tuple(m.get("weak_patterns", [])),
                type=str(m.get("type", "unknown")), connection=str(m.get("connection", "unknown")),
                expected_speech_dbfs=None if lvl is None else float(lvl),
                best_host_api=str(m.get("best_host_api", "")), exclusive=bool(m.get("exclusive", False)),
                notes=str(m.get("notes", "")), blocksize=block, latency=lat,
                auto_convert=bool(m.get("auto_convert", True)),
                quiet_tip=str(m.get("quiet_tip", base.quiet_tip)), loud_tip=str(m.get("loud_tip", base.loud_tip)),
                room_tip=str(m.get("room_tip", base.room_tip))))
    except FileNotFoundError:
        profiles = []
    except Exception:
        log.exception("could not read %s; using the built-in mic profiles", p)
        profiles = []
    if not profiles:
        profiles = list(BUILTIN_PROFILES)
    if not any(x.is_generic for x in profiles):
        profiles.append(GENERIC)
    return sorted(profiles, key=lambda x: (x.is_generic, x.priority))


def get_profile(key: str | None, profiles: list[MicProfile] | None = None) -> MicProfile | None:
    """Profile by id or display name (case-insensitive); None if unknown."""
    if not key:
        return None
    k = key.strip().lower()
    for p in profiles if profiles is not None else load_profiles():
        if k in (p.id.lower(), p.name.lower()):
            return p
    return None


# ------------------------------------------------------------------------------ device names

def clean_name(name: str) -> str:
    """'Microphone (2- Samson Q2U Microphone)' -> 'Samson Q2U Microphone' (for display + dedupe)."""
    n = name.strip()
    m = re.fullmatch(r"[^()]*\((.+)\)\s*", n)            # "<endpoint> (<device>)" -> device
    if m:
        n = m.group(1)
    elif "(" in n and not n.endswith(")"):              # MME truncated: "Microphone (Samson Q2U Micropho"
        n = n.split("(", 1)[1]
    n = re.sub(r"^\d+\s*-\s*", "", n.strip())           # "2- " prefix Windows adds for duplicates
    return n.strip() or name.strip()


def input_names(devices: list[dict], hostapis: list[dict] | None = None) -> list[str]:
    """Every input device name across all host APIs, deduped (truncated MME names fold into the full one)."""
    names: list[str] = []
    for d in devices:
        if d.get("max_input_channels", 0) <= 0:
            continue
        raw = str(d.get("name", ""))
        n = clean_name(raw)
        if not n or _NOT_A_MIC.search(raw):
            continue
        key = n.lower()
        dup = next((i for i, x in enumerate(names) if x.lower().startswith(key) or key.startswith(x.lower())), None)
        if dup is None:
            names.append(n)
        elif len(n) > len(names[dup]):
            names[dup] = n                                   # keep the untruncated spelling
    return names


# ------------------------------------------------------------------------------ detection

@dataclass
class Detection:
    profile: MicProfile
    device_name: str = ""               # the input that matched ("" for Generic / override with no device)
    source: str = "auto"                # auto | override | fallback
    found: list[tuple[str, str]] = field(default_factory=list)   # (device name, profile id) for every input
    unmatched: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.source == "override":
            return f"{self.profile.name} (chosen by hand)"
        if self.profile.is_generic:
            return "Generic mic (no known mic found)" if self.source == "auto" else "Generic mic"
        return f"{self.profile.name} ({self.device_name})"


_logged_unmatched: set[str] = set()


def _query():
    import sounddevice as sd
    return list(sd.query_devices()), list(sd.query_hostapis())


def detect(devices: list[dict] | None = None, hostapis: list[dict] | None = None,
           override: str | None = None, profiles: list[MicProfile] | None = None) -> Detection:
    """Full detection result; never raises. `devices`/`hostapis` = sounddevice lists (None = query now)."""
    profiles = profiles if profiles is not None else load_profiles()
    generic = next((p for p in profiles if p.is_generic), GENERIC)
    if devices is None:
        try:
            devices, hostapis = _query()
        except Exception as e:
            log.info("mic detection: could not list devices (%s); using Generic", e)
            devices = []
    names = input_names(devices, hostapis)

    best: tuple[int, int, MicProfile, str] | None = None           # (-strength, priority, profile, device)
    found, unmatched = [], []
    for n in names:
        hits = [(p.match(n), p) for p in profiles if not p.is_generic]
        hits = [(s, p) for s, p in hits if s]
        if not hits:
            found.append((n, generic.id))
            unmatched.append(n)
            continue
        s, p = max(hits, key=lambda h: (h[0], -h[1].priority))
        found.append((n, p.id))
        cand = (-s, p.priority, p, n)
        if best is None or cand[:2] < best[:2]:
            best = cand

    for n in unmatched:
        if n not in _logged_unmatched:
            _logged_unmatched.add(n)
            log.info("mic detection: unknown input %r (add a pattern to config/mics.toml if it is a real mic)", n)

    if override and override.strip().lower() != "auto":
        p = get_profile(override, profiles)
        if p is not None:
            dev = next((n for n, pid in found if pid == p.id), "")
            return Detection(p, dev, "override", found, unmatched)
        log.info("mic profile override %r is unknown; detecting instead", override)

    if best is None:
        return Detection(generic, "", "auto" if names else "fallback", found, unmatched)
    if best[0] == -1:
        log.info("mic detection: %r is probably a %s (generic name)", best[3], best[2].name)
    return Detection(best[2], best[3], "auto", found, unmatched)


def detect_mic(devices: list[dict] | None = None, hostapis: list[dict] | None = None,
               override: str | None = None, profiles: list[MicProfile] | None = None) -> MicProfile:
    """The profile of the mic that is plugged in (Generic if unknown). See `detect` for details."""
    return detect(devices, hostapis, override, profiles).profile


def profile_for_device(name: str, profiles: list[MicProfile] | None = None) -> MicProfile:
    """Profile for one device name (e.g. the mic chosen in the UI's picker); Generic if unknown."""
    profiles = profiles if profiles is not None else load_profiles()
    hits = [(p.match(clean_name(name)) or p.match(name), p) for p in profiles if not p.is_generic]
    hits = [(s, p) for s, p in hits if s]
    if not hits:
        return next((p for p in profiles if p.is_generic), GENERIC)
    return max(hits, key=lambda h: (h[0], -h[1].priority))[1]
