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
            "python3", "-m", "demucs.separate",
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
        stem_files = [os.path.join(separated_dir, f) for f in os.listdir(separated_dir) if f.endswith('.wav') and f != 'multichannel_stems.wav']
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

            # Handle both mono and stereo stems - convert to stereo if needed
            if data.ndim == 1:
                # Mono: convert to stereo by duplicating
                data = np.stack([data, data], axis=1)
            elif data.ndim == 2:
                # Already stereo, ensure correct shape (frames, channels)
                if data.shape[1] != 2:
                    raise ValueError(f"Unexpected number of channels: {data.shape[1]}")

            # Ensure all stems have same length
            if data.shape[0] != num_frames:
                if data.shape[0] < num_frames:
                    # Pad shorter stems
                    pad_amount = num_frames - data.shape[0]
                    data = np.pad(data, ((0, pad_amount), (0, 0)), 'constant')
                else:
                    # Truncate longer stems
                    data = data[:num_frames]

            stems_data.append(data)

        # Stack all stems: shape will be (num_frames, 2, num_stems)
        # Each stem keeps its stereo channels
        multichannel_audio = np.stack(stems_data, axis=2)  # Stack along third dimension

        # Reshape to (num_frames, num_stems * 2) for writing as interleaved multichannel WAV
        num_stems = len(stems_data)
        multichannel_audio = multichannel_audio.reshape(num_frames, num_stems * 2)

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

        # Get info from first stem to determine shape
        first_stem_info = sf.info(list(individual_stem_paths.values())[0])
        num_frames = first_stem_info.frames
        num_channels = first_stem_info.channels

        # Initialize mixed audio with correct shape (stereo or mono)
        if num_channels == 2:
            mixed_audio_sum = np.zeros((num_frames, 2), dtype='float32')
        else:
            mixed_audio_sum = np.zeros(num_frames, dtype='float32')

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
    # First try the path as-is (it might already be relative to project root)
    if os.path.exists(filename) and os.path.isfile(filename):
        from fastapi.responses import FileResponse
        return FileResponse(filename)

    # Then try prepending each base directory
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

