# Hush Studio: third-party software in this installer

Hush Studio is a personal tool. The installer bundles the software below, unchanged, each under
its own licence. Checked for T21 on 2026-11-11 against the versions in the build venv; re-check
when a dependency changes major version.

| Component | Version (at check) | Licence | What it means for us |
|---|---|---|---|
| Python (CPython runtime) | 3.12 | PSF License 2.0 | Permissive; keep this notice. |
| Microsoft VC++ runtime (`vcruntime140.dll`, beside deep-filter.exe and Python) | 14.x | Microsoft Visual C++ Redistributable licence | Redistributable with an application. |
| PyQt6 | 6.12 | **GPL-3.0** (or Riverbank commercial) | Fine for a personal/free tool. If Hush Studio is ever **sold or shared as closed source**, buy a PyQt commercial licence or move to PySide6 (LGPL). |
| Qt 6 (PyQt6-Qt6) | 6.12 | LGPL-3.0 | Shipped as separate, replaceable DLLs (one-folder build), which LGPL requires. |
| PyQt6-sip | 13.x | BSD-2-Clause | Permissive. |
| NumPy (+ bundled OpenBLAS) | 2.x | BSD-3-Clause (+ BSD/MIT/Zlib parts) | Permissive. |
| SciPy (+ OpenBLAS, gfortran runtime) | 1.x | BSD-3-Clause; libgfortran GPL-3.0 **with the runtime library exception** | Permissive in practice. |
| soundfile + libsndfile | 0.14 / 1.2 | BSD-3-Clause; libsndfile **LGPL-2.1** | libsndfile ships as a separate DLL (replaceable). |
| sounddevice + PortAudio | 0.5 / v19.7 | MIT; PortAudio MIT-style | Permissive. |
| pyloudnorm | 0.2 | MIT | Permissive. |
| google-auth, google-auth-oauthlib | 2.x / 1.x | Apache-2.0 | Permissive; keep notices. |
| requests, requests-oauthlib, oauthlib | 2.x / 2.x / 3-4.x | Apache-2.0 / ISC / BSD-3-Clause | Permissive. |
| urllib3, idna, charset-normalizer, pyasn1(-modules), cffi, pycparser | various | MIT / BSD | Permissive. |
| certifi (CA bundle) | 2026.x | MPL-2.0 | Unmodified file; fine. |
| **FFmpeg** (`tools/ffmpeg/ffmpeg.exe`, `ffprobe.exe`; gyan.dev static build) | ≥ 6 | **GPL-3.0** (built with libx264 and other GPL parts); its LICENSE ships beside it | Fine to give away. Anyone who receives the installer may ask for the corresponding source: point them to https://ffmpeg.org/download.html and the build's README (gyan.dev publishes its build configuration). Do not sell a closed-source product with this build inside. |
| x264 (inside FFmpeg) | – | GPL-2.0+ | As FFmpeg above. |
| **DeepFilterNet** `deep-filter.exe` | 0.5.6 | MIT OR Apache-2.0 (dual) | Permissive. Model weights included in the exe under the same licence. |
| Montserrat font | – | SIL Open Font License 1.1 (`assets/fonts/OFL.txt`) | Free to bundle; not sold on its own. |
| App icon | – | Drawn in code (`build/make_icon.py`) | Ours. |
| Companion GIFs (`assets/companion_*.gif`, only if present) | – | **Third-party character artwork (Pokémon / Nintendo IP)** | Personal use only. Swap for an original mascot before the app is ever shared, sold or shown on the channel (CLAUDE.md §6). |

Summary: everything is fine for Sam's personal use and for giving the installer away for free.
Two things would need changing before **selling** Hush Studio: PyQt6 (GPL → commercial licence or
PySide6) and the GPL FFmpeg build (offer source, or use an LGPL build without libx264 and encode the
black loop another way), plus the companion artwork.
