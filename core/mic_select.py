"""Pick the best way to open the microphone (Samson Q2U first).

Windows exposes one USB mic through several host APIs. MME / DirectSound / WASAPI-shared go
through the Windows audio engine (resampling, optional auto-gain and "enhancements"). WASAPI
*exclusive* talks to the device directly, so what the mic sends is what we record. Exclusive blocks
other apps from the mic while recording, so we fall back to shared mode if it won't open.
"""
from __future__ import annotations

from dataclasses import dataclass

PREFERRED_NAMES = ("q2u", "samson")          # case-insensitive substrings; Sam's mic
API_RANK = {"Windows WASAPI": 0, "Windows WDM-KS": 1, "Windows DirectSound": 2, "MME": 3}


@dataclass(frozen=True)
class Candidate:
    device: int
    exclusive: bool
    label: str


def _is_preferred(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in PREFERRED_NAMES)


def candidates(devices: list[dict], hostapis: list[dict], device: int | str | None = None) -> list[Candidate]:
    """Ordered ways to try opening the mic. `device` = explicit choice (index/name) or None = auto."""
    out: list[Candidate] = []
    ins = [(i, d) for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]

    def api(d):
        return hostapis[d["hostapi"]]["name"]

    if isinstance(device, int):
        chosen = [(i, d) for i, d in ins if i == device]
    elif isinstance(device, str) and device:
        chosen = [(i, d) for i, d in ins if device.lower() in d["name"].lower()]
    else:
        chosen = [(i, d) for i, d in ins if _is_preferred(d["name"])]
    if not chosen:
        return []                                  # caller falls back to the system default

    # Same physical mic as the chosen one, on every host API, best API first.
    names = {d["name"].split("(")[-1].strip(") ").lower() or d["name"].lower() for _, d in chosen}
    same = [(i, d) for i, d in ins if any(n and n in d["name"].lower() for n in names)] or chosen
    same.sort(key=lambda t: API_RANK.get(api(t[1]), 9))
    for i, d in same:
        if api(d) == "Windows WASAPI":
            out.append(Candidate(i, True, "WASAPI exclusive (direct from the mic)"))
            out.append(Candidate(i, False, "WASAPI shared"))
        elif api(d) in API_RANK:
            out.append(Candidate(i, False, api(d)))
    return out
