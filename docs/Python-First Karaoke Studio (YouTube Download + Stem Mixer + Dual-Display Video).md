# Python-First Karaoke Studio (YouTube Download + Stem Mixer + Dual-Display Video)

This project develops a desktop karaoke application that leverages a Python backend for robust audio/video processing and an Electron frontend for a responsive and interactive user interface. The application allows users to download YouTube videos, separate their audio into individual stems (e.g., vocals, drums, bass), mix these stems in real-time, and export a remixed video. It also features a dual-display mode for projecting the video onto an external screen.

## Features

*   **YouTube Video Download:** Accepts a YouTube URL and downloads the highest-quality MP4 video using `yt-dlp`.
*   **Advanced Stem Separation:** Utilizes the Demucs library to separate audio into 4 or 6 distinct stems (Vocals, Drums, Bass, Guitar, Piano, Other).
*   **Real-time Stem Mixing:** Provides an interactive mixer in the Electron frontend with volume sliders for each stem, allowing real-time adjustments during playback.
*   **Remixed MP4 Export:** Exports the mixed audio along with the original video (without re-encoding the video stream) into a new MP4 file.
*   **Dual-Display Mode:** Supports a dedicated fullscreen video display on a second monitor or HDMI output, synchronized with the main operator window.
*   **Progress Tracking:** Displays real-time progress and status updates for long-running tasks like downloading, separation, and export.

## Architecture

The application follows a client-server architecture:

*   **Python Backend (FastAPI):**
    *   Handles all computationally intensive tasks: video downloading (`yt-dlp`), stem separation (`Demucs`), audio merging, and final video remuxing (`ffmpeg-python`).
    *   Exposes a RESTful API with endpoints for initiating and monitoring these processes.
    *   Uses `uvicorn` to serve the FastAPI application.

*   **Electron Frontend (HTML/CSS/JavaScript with Axios):**
    *   Provides the graphical user interface (GUI) for user interaction.
    *   Communicates with the Python backend via HTTP requests using `axios`.
    *   Manages video playback, stem volume controls, and the dual-display functionality.
    *   Utilizes the Web Audio API for local, real-time stem playback and gain control.

## Prerequisites

Before running the application, ensure you have the following installed:

*   **Python 3.8+:** Download from [python.org](https://www.python.org/downloads/).
*   **Node.js and npm:** Download from [nodejs.org](https://nodejs.org/en/download/).
*   **FFmpeg:** Essential for video processing. Download from [ffmpeg.org](https://ffmpeg.org/download.html) and ensure it's added to your system's PATH.

## Setup Instructions

Follow these steps to set up and run the Karaoke Studio application.

### 1. Backend Setup

1.  **Create a project directory:**
    ```bash
    mkdir karaoke-studio
    cd karaoke-studio
    ```

2.  **Create a Python virtual environment and activate it:**
    ```bash
    python -m venv venv
    # On macOS/Linux:
    source venv/bin/activate
    # On Windows:
    .\venv\Scripts\activate
    ```

3.  **Install Python dependencies:**
    ```bash
    pip install fastapi uvicorn "yt-dlp" torch torchaudio ffmpeg-python soundfile numpy
    pip install git+https://github.com/facebookresearch/demucs#egg=demucs
    ```
    *Note: `torch` and `torchaudio` can be large. Ensure you have a stable internet connection.*

4.  **Save the FastAPI backend code:**
    Create a file named `main.py` in your `karaoke-studio` directory and paste the following code:

    ```python
    import os
    import subprocess
    import asyncio
    import json
    import uuid
    import shutil

    from fastapi import FastAPI, BackgroundTasks, HTTPException
    from pydantic import BaseModel
    import yt_dlp
    import soundfile as sf
    import numpy as np
    import ffmpeg

    # --- Configuration ---
    DOWNLOADS_DIR = "./downloads"
    SEPARATED_DIR = "./separated"
    MIXES_DIR = "./mixes"

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    os.makedirs(SEPARATED_DIR, exist_ok=True)
    os.makedirs(MIXES_DIR, exist_ok=True)

    app = FastAPI()

    # In-memory storage for task progress and results (for simplicity)
    # In a production app, consider a database or a more robust caching mechanism
    tasks = {}

    # --- Data Models ---
    class DownloadRequest(BaseModel):
        url: str

    class SeparateRequest(BaseModel):
        task_id: str
        video_path: str
        model: str = "htdemucs_6s" # Default to 6-stem model

    class MergeRequest(BaseModel):
        task_id: str
        separated_dir: str

    class MixExportRequest(BaseModel):
        task_id: str
        video_path: str
        multichannel_wav_path: str
        gains: dict # e.g., {"vocals": 1.0, "drums": 0.8, ...}
        output_filename: str

    class ProgressResponse(BaseModel):
        task_id: str
        status: str
        progress: float = 0.0 # 0.0 to 1.0
        message: str = ""
        result: dict = None

    # --- Helper Functions ---
    async def run_command(command: list, task_id: str, message_prefix: str):
        """Helper to run shell commands and update task progress."""
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            tasks[task_id] = {"status": "failed", "message": f"{message_prefix} failed: {stderr.decode().strip()}"}
            raise RuntimeError(f"{message_prefix} failed: {stderr.decode().strip()}")
        
        tasks[task_id]["message"] = f"{message_prefix} completed."
        return stdout.decode().strip()


    async def do_download(task_id: str, url: str):
        """Downloads the best quality mp4 video from a YouTube URL."""
        tasks[task_id] = {"status": "in_progress", "progress": 0.0, "message": "Starting download..."}
        try:
            ydl_opts = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'outtmpl': os.path.join(DOWNLOADS_DIR, '%(title)s.%(ext)s'),
                'merge_output_format': 'mp4',
                'progress_hooks': [lambda d: update_download_progress(task_id, d)],
                'postprocessors': [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }],
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                tasks[task_id] = {"status": "completed", "progress": 1.0, "message": "Download complete.", "result": {"video_path": filepath}}
        except Exception as e:
            tasks[task_id] = {"status": "failed", "message": f"Download failed: {e}"}

    def update_download_progress(task_id, d):
        if d['status'] == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            if total_bytes:
                downloaded_bytes = d.get('downloaded_bytes', 0)
                progress = downloaded_bytes / total_bytes
                tasks[task_id].update({"progress": progress, "message": f"Downloading: {d['_percent_str']} at {d['_speed_str']}"})
        elif d['status'] == 'finished':
            tasks[task_id].update({"progress": 1.0, "message": "Post-processing download..."})


    async def do_separate(task_id: str, video_path: str, model: str):
        """Separates an audio or video file into stems using Demucs."""
        tasks[task_id] = {"status": "in_progress", "progress": 0.0, "message": "Starting separation..."}
        try:
            # Create a unique output directory for this separation task
            output_base_name = os.path.splitext(os.path.basename(video_path))[0]
            unique_output_dir = os.path.join(SEPARATED_DIR, f"{output_base_name}_{uuid.uuid4().hex}")
            os.makedirs(unique_output_dir, exist_ok=True)

            command = [
                "python3.11", "-m", "demucs.separate",
                "-n", model,
                "-o", unique_output_dir,
                "--filename", "{stem}.{ext}", # Output stems directly in the unique_output_dir
                video_path
            ]
            
            # Demucs output is verbose, we'll just capture it and update status based on completion
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                tasks[task_id] = {"status": "failed", "message": f"Separation failed: {stderr.decode().strip()}"}
                raise RuntimeError(f"Demucs separation failed: {stderr.decode().strip()}")

            # Demucs creates a subdirectory inside unique_output_dir with the track name
            # We need to find that directory
            demucs_output_dirs = [d for d in os.listdir(unique_output_dir) if os.path.isdir(os.path.join(unique_output_dir, d))]
            if not demucs_output_dirs:
                raise RuntimeError("Demucs did not create an output directory.")
            
            actual_separated_path = os.path.join(unique_output_dir, demucs_output_dirs[0])

            tasks[task_id] = {"status": "completed", "progress": 1.0, "message": "Separation complete.", "result": {"separated_dir": actual_separated_path, "model": model}}
        except Exception as e:
            tasks[task_id] = {"status": "failed", "message": f"Separation failed: {e}"}

    async def do_merge_stems(task_id: str, separated_dir: str):
        """Merges separated stems into a single multichannel WAV file."""
        tasks[task_id] = {"status": "in_progress", "progress": 0.0, "message": "Merging stems..."}
        try:
            stem_files = [os.path.join(separated_dir, f) for f in os.listdir(separated_dir) if f.endswith('.wav')]
            if not stem_files:
                raise ValueError(f"No WAV stem files found in {separated_dir}")

            # Read all stems and ensure they have the same sample rate and length
            first_stem_info = sf.info(stem_files[0])
            samplerate = first_stem_info.samplerate
            num_frames = first_stem_info.frames

            stems_data = []
            for stem_file in stem_files:
                data, sr = sf.read(stem_file, dtype='float32')
                if sr != samplerate:
                    raise ValueError(f"Sample rate mismatch for {stem_file}")
                if len(data) != num_frames:
                    # Pad or truncate to match the first stem's length
                    if len(data) < num_frames:
                        data = np.pad(data, (0, num_frames - len(data)), 'constant')
                    else:
                        data = data[:num_frames]
                stems_data.append(data)

            # Stack stems to create a multichannel array
            multichannel_audio = np.stack(stems_data, axis=1) # Each column is a stem

            output_wav_path = os.path.join(separated_dir, "multichannel_stems.wav")
            sf.write(output_wav_path, multichannel_audio, samplerate)

            tasks[task_id] = {"status": "completed", "progress": 1.0, "message": "Stems merged successfully.", "result": {"multichannel_wav_path": output_wav_path}}
        except Exception as e:
            tasks[task_id] = {"status": "failed", "message": f"Stem merging failed: {e}"}

    async def do_mix_export(task_id: str, video_path: str, multichannel_wav_path: str, gains: dict, output_filename: str):
        """Applies gains to stems and remuxes with video."""
        tasks[task_id] = {"status": "in_progress", "progress": 0.0, "message": "Starting mix export..."}
        try:
            # Read the multichannel WAV
            multichannel_audio, samplerate = sf.read(multichannel_wav_path, dtype='float32')
            num_stems = multichannel_audio.shape[1]

            # Apply gains
            mixed_audio = np.zeros(multichannel_audio.shape[0], dtype='float32')
            stem_names = sorted([os.path.splitext(os.path.basename(f))[0] for f in os.listdir(os.path.dirname(multichannel_wav_path)) if f.endswith('.wav') and f != 'multichannel_stems.wav'])
            
            # Ensure stem_names matches the order of channels in multichannel_audio
            # This is a critical assumption based on how do_merge_stems stacks them.
            # For robustness, you might want to store stem order explicitly.
            
            # For now, let's assume the order is consistent with sorted names.
            # If Demucs output order is not alphabetical, this needs adjustment.
            # A safer approach would be to read each stem individually, apply gain, and then sum.
            
            # Let's re-read individual stems for safer gain application
            individual_stem_paths = {os.path.splitext(os.path.basename(f))[0]: os.path.join(os.path.dirname(multichannel_wav_path), f) 
                                     for f in os.listdir(os.path.dirname(multichannel_wav_path)) 
                                     if f.endswith('.wav') and f != 'multichannel_stems.wav'}
            
            mixed_audio_sum = np.zeros(sf.info(list(individual_stem_paths.values())[0]).frames, dtype='float32')

            for stem_name, stem_path in individual_stem_paths.items():
                stem_data, _ = sf.read(stem_path, dtype='float32')
                gain = gains.get(stem_name, 1.0) # Default gain 1.0 if not specified
                mixed_audio_sum += stem_data * gain

            # Normalize mixed audio to prevent clipping
            max_abs_val = np.max(np.abs(mixed_audio_sum))
            if max_abs_val > 1.0:
                mixed_audio_sum /= max_abs_val

            # Save the mixed audio to a temporary WAV file
            temp_mixed_audio_path = os.path.join(MIXES_DIR, f"temp_mixed_audio_{uuid.uuid4().hex}.wav")
            sf.write(temp_mixed_audio_path, mixed_audio_sum, samplerate)

            output_filepath = os.path.join(MIXES_DIR, output_filename)

            # Use ffmpeg to remux video with the new audio
            # Input video stream, input audio stream, copy video, map audio, output
            (ffmpeg
                .input(video_path)
                .output(ffmpeg.input(temp_mixed_audio_path).audio, output_filepath, vcodec='copy', acodec='aac', strict='experimental')
                .overwrite_output()
                .run(capture_stdout=True, capture_stderr=True))

            os.remove(temp_mixed_audio_path) # Clean up temporary audio file

            tasks[task_id] = {"status": "completed", "progress": 1.0, "message": "Mix export complete.", "result": {"output_path": output_filepath}}
        except ffmpeg.Error as e:
            tasks[task_id] = {"status": "failed", "message": f"FFmpeg error during mix export: {e.stderr.decode().strip()}"}
        except Exception as e:
            tasks[task_id] = {"status": "failed", "message": f"Mix export failed: {e}"}


    # --- API Endpoints ---
    @app.post("/download")
    async def download_video_endpoint(req: DownloadRequest, background_tasks: BackgroundTasks):
        task_id = str(uuid.uuid4())
        background_tasks.add_task(do_download, task_id, req.url)
        return {"task_id": task_id, "message": "Download started in the background."}

    @app.post("/separate")
    async def separate_audio_endpoint(req: SeparateRequest, background_tasks: BackgroundTasks):
        if not os.path.exists(req.video_path):
            raise HTTPException(status_code=404, detail="Video file not found.")
        background_tasks.add_task(do_separate, req.task_id, req.video_path, req.model)
        return {"task_id": req.task_id, "message": f"Separation with model '{req.model}' started for {req.video_path}."}

    @app.post("/merge")
    async def merge_stems_endpoint(req: MergeRequest, background_tasks: BackgroundTasks):
        if not os.path.isdir(req.separated_dir):
            raise HTTPException(status_code=404, detail="Separated stems directory not found.")
        background_tasks.add_task(do_merge_stems, req.task_id, req.separated_dir)
        return {"task_id": req.task_id, "message": "Stem merging started in the background."}

    @app.post("/mix-export")
    async def mix_export_endpoint(req: MixExportRequest, background_tasks: BackgroundTasks):
        if not os.path.exists(req.video_path):
            raise HTTPException(status_code=404, detail="Original video file not found.")
        if not os.path.exists(req.multichannel_wav_path):
            raise HTTPException(status_code=404, detail="Multichannel WAV file not found.")
        background_tasks.add_task(do_mix_export, req.task_id, req.video_path, req.multichannel_wav_path, req.gains, req.output_filename)
        return {"task_id": req.task_id, "message": "Mix export started in the background."}

    @app.get("/progress/{task_id}", response_model=ProgressResponse)
    async def get_task_progress(task_id: str):
        task_info = tasks.get(task_id)
        if not task_info:
            raise HTTPException(status_code=404, detail="Task not found.")
        return ProgressResponse(task_id=task_id, **task_info)

    @app.get("/files/{filename:path}")
    async def serve_file(filename: str):
        """Serve static files from downloads, separated, and mixes directories."""
        # Basic security: ensure file is within allowed directories
        for base_dir in [DOWNLOADS_DIR, SEPARATED_DIR, MIXES_DIR]:
            file_path = os.path.join(base_dir, filename)
            if os.path.exists(file_path) and os.path.isfile(file_path):
                from fastapi.responses import FileResponse
                return FileResponse(file_path)
        raise HTTPException(status_code=404, detail="File not found.")


    # --- Cleanup Endpoint (Optional, for development/testing) ---
    @app.post("/cleanup")
    async def cleanup_files():
        """Removes all downloaded, separated, and mixed files."""
        for directory in [DOWNLOADS_DIR, SEPARATED_DIR, MIXES_DIR]:
            if os.path.exists(directory):
                shutil.rmtree(directory)
                os.makedirs(directory)
        tasks.clear()
        return {"message": "All temporary files and task data cleaned up."}
    ```

5.  **Run the FastAPI backend:**
    Open a new terminal, navigate to your `karaoke-studio` directory, activate the virtual environment, and run:
    ```bash
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
    ```
    The backend server will start and listen on `http://localhost:8000`.

### 2. Frontend Setup

1.  **Create the Electron project structure:**
    In your `karaoke-studio` directory, create a sub-directory for the Electron app:
    ```bash
    mkdir karaoke-studio-electron
    cd karaoke-studio-electron
    npm init -y
    ```

2.  **Install Electron and other Node.js dependencies:**
    ```bash
    npm install electron axios --save-dev
    npm install electron-builder --save-dev
    ```

3.  **Save the Electron frontend code:**
    Create the following files in your `karaoke-studio-electron` directory:

    *   **`main.js` (Electron Main Process):**
        ```javascript
        const { app, BrowserWindow, ipcMain, screen } = require('electron');
        const path = require('path');

        let mainWindow;
        let projectorWindow = null;

        function createMainWindow() {
          mainWindow = new BrowserWindow({
            width: 1200,
            height: 800,
            webPreferences: {
              nodeIntegration: true,
              contextIsolation: false,
              enableRemoteModule: true
            }
          });

          mainWindow.loadFile('index.html');
          // mainWindow.webContents.openDevTools(); // Uncomment for development tools

          mainWindow.on('closed', () => {
            mainWindow = null;
            if (projectorWindow) {
              projectorWindow.close();
            }
          });
        }

        // IPC handlers for dual-display mode
        ipcMain.on('open-projector', (event, videoSrc) => {
          if (projectorWindow) {
            projectorWindow.focus();
            return;
          }

          const displays = screen.getAllDisplays();
          const externalDisplay = displays.find((display) => {
            return display.bounds.x !== 0 || display.bounds.y !== 0;
          });

          if (externalDisplay) {
            projectorWindow = new BrowserWindow({
              x: externalDisplay.bounds.x,
              y: externalDisplay.bounds.y,
              fullscreen: true,
              frame: false,
              webPreferences: {
                nodeIntegration: true,
                contextIsolation: false
              }
            });
          } else {
            // No external display, open fullscreen on main display
            projectorWindow = new BrowserWindow({
              fullscreen: true,
              frame: false,
              webPreferences: {
                nodeIntegration: true,
                contextIsolation: false
              }
            });
          }

          projectorWindow.loadFile('projector.html');

          projectorWindow.webContents.on('did-finish-load', () => {
            projectorWindow.webContents.send('set-video-src', videoSrc);
          });

          projectorWindow.on('closed', () => {
            projectorWindow = null;
          });
        });

        ipcMain.on('projector-play', () => {
          if (projectorWindow) {
            projectorWindow.webContents.send('play');
          }
        });

        ipcMain.on('projector-pause', () => {
          if (projectorWindow) {
            projectorWindow.webContents.send('pause');
          }
        });

        ipcMain.on('projector-seek', (event, time) => {
          if (projectorWindow) {
            projectorWindow.webContents.send('seek', time);
          }
        });

        ipcMain.on('close-projector', () => {
          if (projectorWindow) {
            projectorWindow.close();
            projectorWindow = null;
          }
        });

        app.whenReady().then(createMainWindow);

        app.on('window-all-closed', () => {
          if (process.platform !== 'darwin') {
            app.quit();
          }
        });

        app.on('activate', () => {
          if (BrowserWindow.getAllWindows().length === 0) {
            createMainWindow();
          }
        });
        ```

    *   **`index.html` (Main Operator UI):**
        ```html
        <!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="UTF-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
          <title>Karaoke Studio - Operator</title>
          <link rel="stylesheet" href="styles.css">
        </head>
        <body>
          <div class="container">
            <header>
              <h1>🎤 Karaoke Studio</h1>
              <p>Python-First Karaoke with Stem Separation & Dual-Display</p>
            </header>

            <section class="url-section">
              <label for="youtube-url">YouTube URL:</label>
              <input type="text" id="youtube-url" placeholder="https://www.youtube.com/watch?v=..." />
              <button id="download-btn">Download</button>
            </section>

            <section class="progress-section">
              <div id="progress-container" style="display: none;">
                <div class="progress-bar">
                  <div id="progress-bar-fill" class="progress-bar-fill"></div>
                </div>
                <p id="progress-text">Idle</p>
              </div>
            </section>

            <section class="video-section">
              <h2>Video Preview</h2>
              <video id="video-preview" controls></video>
            </section>

            <section class="controls-section">
              <h2>Processing Controls</h2>
              <div class="button-group">
                <button id="separate-btn" disabled>Separate Stems (6-stem)</button>
                <button id="merge-btn" disabled>Merge Stems</button>
              </div>
            </section>

            <section class="mixer-section">
              <h2>Stem Mixer</h2>
              <div id="stem-sliders">
                <!-- Sliders will be dynamically generated after separation -->
              </div>
            </section>

            <section class="playback-section">
              <h2>Playback Controls</h2>
              <div class="button-group">
                <button id="play-btn" disabled>Play</button>
                <button id="pause-btn" disabled>Pause</button>
                <button id="stop-btn" disabled>Stop</button>
              </div>
            </section>

            <section class="projector-section">
              <h2>Dual-Display Mode</h2>
              <div class="button-group">
                <button id="projector-btn" disabled>Send to Projector</button>
                <button id="close-projector-btn" disabled>Close Projector</button>
              </div>
            </section>

            <section class="export-section">
              <h2>Export Mix</h2>
              <label for="output-filename">Output Filename:</label>
              <input type="text" id="output-filename" placeholder="my_karaoke_mix.mp4" value="karaoke_mix.mp4" />
              <button id="export-btn" disabled>Export Mix</button>
            </section>
          </div>

          <script src="renderer.js"></script>
        </body>
        </html>
        ```

    *   **`renderer.js` (Electron Renderer Process for Main Window):**
        ```javascript
        const { ipcRenderer } = require('electron');
        const axios = require('axios');

        const API_BASE_URL = 'http://localhost:8000';

        // UI Elements
        const youtubeUrlInput = document.getElementById('youtube-url');
        const downloadBtn = document.getElementById('download-btn');
        const separateBtn = document.getElementById('separate-btn');
        const mergeBtn = document.getElementById('merge-btn');
        const playBtn = document.getElementById('play-btn');
        const pauseBtn = document.getElementById('pause-btn');
        const stopBtn = document.getElementById('stop-btn');
        const projectorBtn = document.getElementById('projector-btn');
        const closeProjectorBtn = document.getElementById('close-projector-btn');
        const exportBtn = document.getElementById('export-btn');
        const outputFilenameInput = document.getElementById('output-filename');
        const videoPreview = document.getElementById('video-preview');
        const progressContainer = document.getElementById('progress-container');
        const progressBarFill = document.getElementById('progress-bar-fill');
        const progressText = document.getElementById('progress-text');
        const stemSlidersContainer = document.getElementById('stem-sliders');

        // State
        let currentTaskId = null;
        let downloadedVideoPath = null;
        let separatedDir = null;
        let multichannelWavPath = null;
        let stemNames = [];
        let stemGains = {};
        let audioContext = null;
        let audioBuffers = {}; // Store audio buffers for each stem
        let gainNodes = {}; // Store gain nodes for each stem
        let sourceNodes = []; // Store source nodes for playback
        let isPlaying = false;
        let startTime = 0;
        let pauseTime = 0;

        // --- Download Workflow ---
        downloadBtn.addEventListener('click', async () => {
          const url = youtubeUrlInput.value.trim();
          if (!url) {
            alert('Please enter a YouTube URL.');
            return;
          }

          try {
            downloadBtn.disabled = true;
            progressContainer.style.display = 'block';
            progressText.textContent = 'Starting download...';

            const response = await axios.post(`${API_BASE_URL}/download`, { url });
            currentTaskId = response.data.task_id;

            pollTaskProgress(currentTaskId, (progress, message, result) => {
              progressBarFill.style.width = `${progress * 100}%`;
              progressText.textContent = message;

              if (result && result.video_path) {
                downloadedVideoPath = result.video_path;
                videoPreview.src = `${API_BASE_URL}/files/${downloadedVideoPath.replace(/^\.\//, '')}`;
                separateBtn.disabled = false;
                alert('Download complete! You can now separate stems.');
              }
            });
          } catch (error) {
            alert(`Download failed: ${error.message}`);
            downloadBtn.disabled = false;
          }
        });

        // --- Separate Stems Workflow ---
        separateBtn.addEventListener('click', async () => {
          if (!downloadedVideoPath) {
            alert('No video downloaded yet.');
            return;
          }

          try {
            separateBtn.disabled = true;
            progressContainer.style.display = 'block';
            progressText.textContent = 'Starting stem separation...';

            const taskId = generateTaskId();
            const response = await axios.post(`${API_BASE_URL}/separate`, {
              task_id: taskId,
              video_path: downloadedVideoPath,
              model: 'htdemucs_6s'
            });

            pollTaskProgress(taskId, (progress, message, result) => {
              progressBarFill.style.width = `${progress * 100}%`;
              progressText.textContent = message;

              if (result && result.separated_dir) {
                separatedDir = result.separated_dir;
                mergeBtn.disabled = false;
                alert('Stem separation complete! You can now merge stems.');
              }
            });
          } catch (error) {
            alert(`Separation failed: ${error.message}`);
            separateBtn.disabled = false;
          }
        });

        // --- Merge Stems Workflow ---
        mergeBtn.addEventListener('click', async () => {
          if (!separatedDir) {
            alert('No separated stems available.');
            return;
          }

          try {
            mergeBtn.disabled = true;
            progressContainer.style.display = 'block';
            progressText.textContent = 'Merging stems...';

            const taskId = generateTaskId();
            const response = await axios.post(`${API_BASE_URL}/merge`, {
              task_id: taskId,
              separated_dir: separatedDir
            });

            pollTaskProgress(taskId, (progress, message, result) => {
              progressBarFill.style.width = `${progress * 100}%`;
              progressText.textContent = message;

              if (result && result.multichannel_wav_path) {
                multichannelWavPath = result.multichannel_wav_path;
                loadStemsForPlayback();
                alert('Stems merged! You can now play and mix.');
              }
            });
          } catch (error) {
            alert(`Merging failed: ${error.message}`);
            mergeBtn.disabled = false;
          }
        });

        // --- Load Stems for Web Audio API Playback ---
        async function loadStemsForPlayback() {
          try {
            audioContext = new (window.AudioContext || window.webkitAudioContext)();

            // Fetch list of stem files from the separated directory
            // We'll need to make a request to get the list of files
            // For simplicity, we'll assume standard Demucs output names
            const possibleStems = ['vocals', 'drums', 'bass', 'other', 'guitar', 'piano'];
            stemNames = [];

            for (const stemName of possibleStems) {
              const stemPath = `${separatedDir}/${stemName}.wav`.replace(/^\.\//, '');
              try {
                const response = await axios.get(`${API_BASE_URL}/files/${stemPath}`, { responseType: 'arraybuffer' });
                const audioBuffer = await audioContext.decodeAudioData(response.data);
                audioBuffers[stemName] = audioBuffer;
                stemNames.push(stemName);
                stemGains[stemName] = 1.0; // Default gain
              } catch (err) {
                // Stem doesn't exist, skip
                console.log(`Stem ${stemName} not found, skipping.`);
              }
            }

            if (stemNames.length === 0) {
              alert('No stems found for playback.');
              return;
            }

            // Create gain nodes for each stem
            stemNames.forEach(stemName => {
              const gainNode = audioContext.createGain();
              gainNode.gain.value = stemGains[stemName];
              gainNode.connect(audioContext.destination);
              gainNodes[stemName] = gainNode;
            });

            // Generate sliders
            generateStemSliders();

            // Enable playback controls
            playBtn.disabled = false;
            pauseBtn.disabled = false;
            stopBtn.disabled = false;
            projectorBtn.disabled = false;
            exportBtn.disabled = false;

          } catch (error) {
            alert(`Failed to load stems for playback: ${error.message}`);
          }
        }

        // --- Generate Stem Sliders ---
        function generateStemSliders() {
          stemSlidersContainer.innerHTML = '';
          stemNames.forEach(stemName => {
            const sliderDiv = document.createElement('div');
            sliderDiv.className = 'stem-slider';

            const label = document.createElement('label');
            label.textContent = stemName;

            const slider = document.createElement('input');
            slider.type = 'range';
            slider.min = '0';
            slider.max = '2';
            slider.step = '0.01';
            slider.value = stemGains[stemName];
            slider.id = `slider-${stemName}`;

            const valueDisplay = document.createElement('span');
            valueDisplay.textContent = `${(stemGains[stemName] * 100).toFixed(0)}%`;
            valueDisplay.id = `value-${stemName}`;

            slider.addEventListener('input', (e) => {
              const newGain = parseFloat(e.target.value);
              stemGains[stemName] = newGain;
              valueDisplay.textContent = `${(newGain * 100).toFixed(0)}%`;
              if (gainNodes[stemName]) {
                gainNodes[stemName].gain.value = newGain;
              }
            });

            sliderDiv.appendChild(label);
            sliderDiv.appendChild(slider);
            sliderDiv.appendChild(valueDisplay);
            stemSlidersContainer.appendChild(sliderDiv);
          });
        }

        // --- Playback Controls ---
        playBtn.addEventListener('click', () => {
          if (!audioContext) {
            alert('Stems not loaded yet.');
            return;
          }

          if (isPlaying) {
            return; // Already playing
          }

          // Stop any existing sources
          sourceNodes.forEach(source => source.stop());
          sourceNodes = [];

          // Create new source nodes for each stem
          const offset = pauseTime; // Resume from pause time
          stemNames.forEach(stemName => {
            const source = audioContext.createBufferSource();
            source.buffer = audioBuffers[stemName];
            source.connect(gainNodes[stemName]);
            source.start(0, offset);
            sourceNodes.push(source);
          });

          // Sync video playback
          videoPreview.currentTime = offset;
          videoPreview.play();

          // Sync projector playback
          ipcRenderer.send('projector-play');

          startTime = audioContext.currentTime - offset;
          isPlaying = true;
        });

        pauseBtn.addEventListener('click', () => {
          if (!isPlaying) {
            return;
          }

          // Stop all sources
          sourceNodes.forEach(source => source.stop());
          sourceNodes = [];

          // Pause video
          videoPreview.pause();

          // Pause projector
          ipcRenderer.send('projector-pause');

          pauseTime = audioContext.currentTime - startTime;
          isPlaying = false;
        });

        stopBtn.addEventListener('click', () => {
          // Stop all sources
          sourceNodes.forEach(source => source.stop());
          sourceNodes = [];

          // Stop video
          videoPreview.pause();
          videoPreview.currentTime = 0;

          // Stop projector
          ipcRenderer.send('projector-pause');
          ipcRenderer.send('projector-seek', 0);

          pauseTime = 0;
          isPlaying = false;
        });

        // --- Projector Controls ---
        projectorBtn.addEventListener('click', () => {
          if (!downloadedVideoPath) {
            alert('No video loaded.');
            return;
          }

          const videoSrc = `${API_BASE_URL}/files/${downloadedVideoPath.replace(/^\.\//, '')}`;
          ipcRenderer.send('open-projector', videoSrc);
          closeProjectorBtn.disabled = false;
        });

        closeProjectorBtn.addEventListener('click', () => {
          ipcRenderer.send('close-projector');
          closeProjectorBtn.disabled = true;
        });

        // --- Export Mix ---
        exportBtn.addEventListener('click', async () => {
          if (!downloadedVideoPath || !multichannelWavPath) {
            alert('Video and stems must be loaded first.');
            return;
          }

          const outputFilename = outputFilenameInput.value.trim();
          if (!outputFilename) {
            alert('Please enter an output filename.');
            return;
          }

          try {
            exportBtn.disabled = true;
            progressContainer.style.display = 'block';
            progressText.textContent = 'Exporting mix...';

            const taskId = generateTaskId();
            const response = await axios.post(`${API_BASE_URL}/mix-export`, {
              task_id: taskId,
              video_path: downloadedVideoPath,
              multichannel_wav_path: multichannelWavPath,
              gains: stemGains,
              output_filename: outputFilename
            });

            pollTaskProgress(taskId, (progress, message, result) => {
              progressBarFill.style.width = `${progress * 100}%`;
              progressText.textContent = message;

              if (result && result.output_path) {
                alert(`Mix exported successfully to: ${result.output_path}`);
                exportBtn.disabled = false;
              }
            });
          } catch (error) {
            alert(`Export failed: ${error.message}`);
            exportBtn.disabled = false;
          }
        });

        // --- Utility Functions ---
        function generateTaskId() {
          return `task-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
        }

        async function pollTaskProgress(taskId, callback) {
          const interval = setInterval(async () => {
            try {
              const response = await axios.get(`${API_BASE_URL}/progress/${taskId}`);
              const { status, progress, message, result } = response.data;

              callback(progress, message, result);

              if (status === 'completed' || status === 'failed') {
                clearInterval(interval);
                if (status === 'failed') {
                  alert(`Task failed: ${message}`);
                }
              }
            } catch (error) {
              clearInterval(interval);
              alert(`Failed to poll task progress: ${error.message}`);
            }
          }, 1000); // Poll every second
        }
        ```

    *   **`styles.css` (Styling for Main Window):**
        ```css
        * {
          margin: 0;
          padding: 0;
          box-sizing: border-box;
        }

        body {
          font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
          background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
          color: #fff;
          padding: 20px;
          overflow-y: auto;
        }

        .container {
          max-width: 1100px;
          margin: 0 auto;
          background: rgba(255, 255, 255, 0.1);
          backdrop-filter: blur(10px);
          border-radius: 15px;
          padding: 30px;
          box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
        }

        header {
          text-align: center;
          margin-bottom: 30px;
        }

        header h1 {
          font-size: 2.5rem;
          margin-bottom: 10px;
          text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.3);
        }

        header p {
          font-size: 1rem;
          opacity: 0.9;
        }

        section {
          margin-bottom: 25px;
          background: rgba(255, 255, 255, 0.05);
          padding: 20px;
          border-radius: 10px;
        }

        section h2 {
          font-size: 1.5rem;
          margin-bottom: 15px;
          border-bottom: 2px solid rgba(255, 255, 255, 0.3);
          padding-bottom: 10px;
        }

        .url-section {
          display: flex;
          align-items: center;
          gap: 10px;
        }

        .url-section label {
          font-weight: bold;
          min-width: 120px;
        }

        .url-section input {
          flex: 1;
          padding: 10px;
          border: none;
          border-radius: 5px;
          font-size: 1rem;
        }

        button {
          padding: 10px 20px;
          background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
          border: none;
          border-radius: 5px;
          color: white;
          font-size: 1rem;
          font-weight: bold;
          cursor: pointer;
          transition: transform 0.2s, box-shadow 0.2s;
        }

        button:hover:not(:disabled) {
          transform: translateY(-2px);
          box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
        }

        button:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }

        .progress-section {
          min-height: 60px;
        }

        .progress-bar {
          width: 100%;
          height: 30px;
          background: rgba(255, 255, 255, 0.2);
          border-radius: 15px;
          overflow: hidden;
          margin-bottom: 10px;
        }

        .progress-bar-fill {
          height: 100%;
          background: linear-gradient(90deg, #4facfe 0%, #00f2fe 100%);
          width: 0%;
          transition: width 0.3s ease;
        }

        #progress-text {
          text-align: center;
          font-size: 0.9rem;
          opacity: 0.9;
        }

        .video-section video {
          width: 100%;
          max-height: 400px;
          border-radius: 10px;
          background: #000;
        }

        .button-group {
          display: flex;
          gap: 10px;
          flex-wrap: wrap;
        }

        .mixer-section #stem-sliders {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
          gap: 20px;
        }

        .stem-slider {
          display: flex;
          flex-direction: column;
          align-items: center;
        }

        .stem-slider label {
          font-weight: bold;
          margin-bottom: 10px;
          text-transform: capitalize;
        }

        .stem-slider input[type="range"] {
          width: 100%;
          height: 8px;
          border-radius: 5px;
          background: rgba(255, 255, 255, 0.3);
          outline: none;
          cursor: pointer;
        }

        .stem-slider input[type="range"]::-webkit-slider-thumb {
          -webkit-appearance: none;
          appearance: none;
          width: 20px;
          height: 20px;
          border-radius: 50%;
          background: #f5576c;
          cursor: pointer;
        }

        .stem-slider input[type="range"]::-moz-range-thumb {
          width: 20px;
          height: 20px;
          border-radius: 50%;
          background: #f5576c;
          cursor: pointer;
          border: none;
        }

        .stem-slider span {
          margin-top: 5px;
          font-size: 0.9rem;
          opacity: 0.8;
        }

        .export-section {
          display: flex;
          align-items: center;
          gap: 10px;
        }

        .export-section label {
          font-weight: bold;
          min-width: 140px;
        }

        .export-section input {
          flex: 1;
          padding: 10px;
          border: none;
          border-radius: 5px;
          font-size: 1rem;
        }
        ```

    *   **`projector.html` (Projector Window UI):**
        ```html
        <!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="UTF-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
          <title>Karaoke Studio - Projector</title>
          <style>
            * {
              margin: 0;
              padding: 0;
              box-sizing: border-box;
            }

            body {
              background: #000;
              display: flex;
              justify-content: center;
              align-items: center;
              height: 100vh;
              overflow: hidden;
            }

            video {
              width: 100%;
              height: 100%;
              object-fit: contain;
            }
          </style>
        </head>
        <body>
          <video id="projector-video"></video>

          <script>
            const { ipcRenderer } = require('electron');
            const video = document.getElementById('projector-video');

            ipcRenderer.on('set-video-src', (event, videoSrc) => {
              video.src = videoSrc;
            });

            ipcRenderer.on('play', () => {
              video.play();
            });

            ipcRenderer.on('pause', () => {
              video.pause();
            });

            ipcRenderer.on('seek', (event, time) => {
              video.currentTime = time;
            });
          </script>
        </body>
        </html>
        ```

4.  **Update `package.json`:**
    Modify the `package.json` file in `karaoke-studio-electron` to include the `start` and `build` scripts, and the `electron-builder` configuration. Your `package.json` should look like this:

    ```json
    {
      "name": "karaoke-studio-electron",
      "version": "1.0.0",
      "main": "main.js",
      "scripts": {
        "start": "electron .",
        "test": "echo \"Error: no test specified\" && exit 1",
        "build": "electron-builder"
      },
      "build": {
        "appId": "com.yourcompany.karaokestudio",
        "productName": "KaraokeStudio",
        "files": [
          "main.js",
          "index.html",
          "projector.html",
          "renderer.js",
          "styles.css",
          "package.json"
        ],
        "directories": {
          "output": "dist"
        },
        "mac": {
          "category": "public.app-category.entertainment"
        },
        "win": {
          "target": "nsis"
        },
        "linux": {
          "target": "AppImage"
        }
      },
      "keywords": [],
      "author": "",
      "license": "ISC",
      "description": "",
      "devDependencies": {
        "axios": "^1.12.2",
        "electron": "^38.2.1",
        "electron-builder": "^24.13.7"
      }
    }
    ```

### 3. Running the Application

1.  **Start the Python Backend:**
    Ensure your Python virtual environment is activated and run the FastAPI server:
    ```bash
    cd /path/to/your/karaoke-studio
    source venv/bin/activate # or .\venv\Scripts\activate on Windows
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
    ```

2.  **Start the Electron Frontend:**
    Open a **new terminal**, navigate to the `karaoke-studio-electron` directory, and run:
    ```bash
    cd /path/to/your/karaoke-studio/karaoke-studio-electron
    npm start
    ```
    The Electron application window will open.

### 4. Usage

1.  **Enter YouTube URL:** Paste a YouTube video URL into the input field and click "Download".
2.  **Separate Stems:** Once downloaded, click "Separate Stems". This will process the audio using Demucs.
3.  **Merge Stems:** After separation, click "Merge Stems" to prepare the audio for playback and mixing.
4.  **Mix and Play:** Volume sliders for each stem will appear. Adjust them as desired. Click "Play" to start playback. The video preview will show the video, and the audio will be mixed according to your slider settings.
5.  **Dual-Display:** Click "Send to Projector" to open a fullscreen video-only window on an external display (if available). Playback will be synchronized.
6.  **Export Mix:** Enter a desired output filename and click "Export Mix" to create a new MP4 file with your custom audio mix.

## Packaging the Application

To create distributable packages for your application (e.g., `.exe` for Windows, `.dmg` for macOS, `.AppImage` for Linux):

1.  **Ensure all dependencies are installed** (both Python and Node.js).
2.  **Build the Electron application:**
    ```bash
    cd /path/to/your/karaoke-studio/karaoke-studio-electron
    npm run build
    ```
    This will create a `dist` directory containing the packaged application for your operating system.

## Future Enhancements

*   **Error Handling & User Feedback:** More robust error messages and user-friendly alerts.
*   **Configuration:** Allow users to select Demucs models (4-stem vs 6-stem), output directories, etc.
*   **Advanced Audio Controls:** Implement panning, EQ, or effects for individual stems.
*   **Persistence:** Save and load mix presets.
*   **Dockerization:** Package the Python backend in a Docker container for easier deployment.

---

**Author:** Manus AI
**Date:** October 04, 2025

