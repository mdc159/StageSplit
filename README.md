# StageSplit

StageSplit is a Python-first karaoke workstation that combines a FastAPI backend with an Electron front-end. It downloads music videos from YouTube, separates them into stems with Demucs, and lets you remix, export, and project a karaoke-ready performance from within a dual-display operator console.

## ✨ Features

- **YouTube ingestion** – Grab full-length music videos with `yt-dlp` directly from the operator UI.
- **Automatic stem separation** – Run Demucs (default `htdemucs_6s`) to generate vocals, drums, bass, guitar, piano, and other stems.
- **Metadata-rich multichannel pipeline** – Backend validates RMS, enforces channel layouts, and publishes stem order for the renderer.
- **Mixer-first playback** – Web Audio API drives live stem gains while the underlying video stays muted for perfect lip-sync.
- **Dual-display projector mode** – Send the lyric video to an external display while keeping mixing controls private.
- **One-click export** – Render a karaoke mix back to MP4 with custom gain staging.

## 🧰 Architecture Overview

| Layer | Technology | Purpose |
| --- | --- | --- |
| Backend | Python 3 · FastAPI · Demucs · ffmpeg-python | Task orchestration (download, separation, multichannel assembly, export) |
| Frontend | Electron · Node.js · Web Audio API | Operator console, stem mixer UI, projector sync |
| Media tooling | ffmpeg / ffprobe CLI, Demucs models | Heavy lifting for separation, channel layout, and remux |

## ✅ Requirements

- **Operating system:** Linux or macOS (Windows works with WSL for Demucs).
- **Python:** 3.10 or newer. Virtual environments are recommended.
- **Node.js:** v18+ (tested with Node 20) plus `npm`.
- **System packages:** `ffmpeg`, `ffprobe`, and `sox` (optional but useful for inspection).
- **Python packages:** install with `pip install fastapi uvicorn[standard] yt-dlp numpy soundfile ffmpeg-python demucs`.
- **Electron dependencies:** install with `npm install`.
- **Demucs models:** The first run will download models automatically; ensure ~4 GB free disk space.

## 🚀 Getting Started

### 1. Clone & enter the repo

```bash
git clone https://github.com/mdc159/StageSplit.git
cd StageSplit
```

### 2. Bootstrap the backend environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn[standard] yt-dlp numpy soundfile ffmpeg-python demucs
```

### 3. Install Electron dependencies

```bash
npm install
```

### 4. Verify `ffmpeg` availability

```bash
ffmpeg -version
ffprobe -version
```

## 🏃‍♀️ Running the App

Start the FastAPI backend (this exposes the REST API used by the renderer):

```bash
uvicorn main:app --reload
```

In a second terminal, launch the Electron operator console:

```bash
npm start
```

> **Tip:** On older GPUs or headless environments, hardware acceleration is disabled automatically, so no extra flags are required. If you still see GPU errors, export `ELECTRON_DISABLE_GPU=1` before `npm start`.

## 🎚️ Typical Workflow

1. **Download** – Paste a YouTube URL and start the download. Progress updates appear under the progress bar.
2. **Separate** – Click *Separate Stems*. Demucs produces stems and a `multichannel_stems.wav`; the backend also records `stem_index.json` with ordering/layout metadata.
3. **Play** – Once separation finishes, hit *Play*. The mixer sliders control individual stem gains while video playback is muted for audio.
4. **Project** – Use *Send to Projector* to mirror the video on a second display. Seek, pause, and stop remain synchronized.
5. **Export** – Choose an output filename, tweak the gains, and press *Export Mix* to produce an MP4 in the `mixes/` directory.

## 📂 Key Directories

| Folder | Purpose |
| --- | --- |
| `downloads/` | Original YouTube MP4 files |
| `separated/` | Demucs outputs (including nested stem folders) |
| `remuxed/` | Auto-remuxed video with aligned multichannel WAV |
| `mixes/` | Exported karaoke mixes |
| `docs/` | Design notes, playback investigation logs |

## 🧪 Verifying Playback

1. Run the backend (`uvicorn main:app --reload`) and the renderer (`npm start`).
2. Use the provided sample videos in `downloads/` or add a fresh URL.
3. After separation, confirm the mixer sliders visibly change relative stem volumes. The video element stays muted by design.
4. Watch the terminal logs: the backend will emit task progress, and the renderer prints `Main window loaded` plus projector-synchronization events.

To perform a quick health check without full UI interaction:

```bash
python3 -m compileall main.py
xvfb-run -a --server-args='-screen 0 1280x720x24' npm start
```
 
Use `Ctrl+C` to stop each process when done.

## 🛠️ Troubleshooting

| Symptom | Likely Cause | Fix |
| --- | --- | --- |
| `Task failed: Separation failed: 'NoneType' object is not a mapping` | Backend lost metadata for the current `task_id` (often when the process crashed or DEMUCS_ENV not set). | Check the backend logs; restart `uvicorn` and retry. Ensure Demucs dependencies are installed and there's enough disk space. |
| GPU/Vulkan warnings during `npm start` | Legacy GPU drivers | Already mitigated by disabling GPU compositing. You can also export `ELECTRON_ENABLE_LOGGING=1` to inspect startup. |
| Slider changes do nothing | `stem_index.json` missing or silent stems | Verify separation completed; the backend rejects silent stems. Re-run separation and inspect the files in `separated/.../htdemucs_6s/`. |
| Exports missing vocals | Export gains were zeroed | Double-check mixer values before exporting; they are used directly for render gains. |

Collect backend logs with:

```bash
uvicorn main:app --reload --log-level debug
```

## 🤝 Contributing

1. Create a feature branch from `dev`.
2. Keep Python formatting (`black` compatible) and JavaScript formatting (2-space indent).
3. Run `python3 -m compileall main.py` after backend changes and smoke test the renderer with `npm start`.
4. Submit a pull request against `main` with a summary of workflow verification.

## 📜 License

This repository currently declares the `ISC` license in `package.json`. Confirm licensing expectations with the project owner before redistribution.

Happy mixing! 🎤
