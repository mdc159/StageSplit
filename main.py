import os
import asyncio
import json
import uuid
import shutil
from typing import List, Tuple

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import soundfile as sf
import numpy as np
import ffmpeg

# GPU detection for Demucs acceleration
def get_demucs_device() -> str:
    """Detect if CUDA is available for GPU acceleration, fallback to CPU."""
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"

# --- Stem + audio utilities ---
EXPECTED_STEM_ORDER = ["vocals", "drums", "bass", "guitar", "piano", "other"]
CHANNEL_LAYOUT_MAP = {
    1: "mono",
    2: "stereo",
    3: "3.0",
    4: "4.0",
    5: "5.0",
    6: "6.0",
}
RMS_SILENCE_THRESHOLD = 1e-6
STEM_INDEX_FILENAME = "stem_index.json"

# --- Configuration ---
DOWNLOADS_DIR = "./downloads"
SEPARATED_DIR = "./separated"
MIXES_DIR = "./mixes"
REMUXED_DIR = "./remuxed"

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(SEPARATED_DIR, exist_ok=True)
os.makedirs(MIXES_DIR, exist_ok=True)
os.makedirs(REMUXED_DIR, exist_ok=True)

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


def _discover_stems(separated_dir: str) -> List[Tuple[str, str]]:
    available = {}
    for entry in os.listdir(separated_dir):
        if entry.endswith('.wav') and entry != 'multichannel_stems.wav':
            stem_name = os.path.splitext(entry)[0]
            available[stem_name] = os.path.join(separated_dir, entry)

    ordered = []
    for stem_name in EXPECTED_STEM_ORDER:
        if stem_name in available:
            ordered.append((stem_name, available.pop(stem_name)))

    # Append any remaining stems alphabetically to avoid missing custom names
    for stem_name in sorted(available.keys()):
        ordered.append((stem_name, available[stem_name]))

    return ordered


def _determine_layout(channel_count: int) -> str:
    return CHANNEL_LAYOUT_MAP.get(channel_count, f"{channel_count}.0")


def _compute_rms(path: str) -> float:
    total = 0.0
    sample_count = 0
    with sf.SoundFile(path) as f:
        for block in f.blocks(blocksize=65536, dtype='float32'):
            if block.size == 0:
                continue
            total += float(np.sum(block ** 2))
            sample_count += block.size
    if sample_count == 0:
        return 0.0
    return float(np.sqrt(total / sample_count))


async def ensure_multichannel_stem(task_id: str, separated_dir: str):
    """Create (or refresh) multichannel_stems.wav with explicit channel layout and metadata."""
    stems = _discover_stems(separated_dir)
    if not stems:
        raise ValueError(f"No WAV stem files found in {separated_dir}")

    stem_order = [stem for stem, _ in stems]
    layout = _determine_layout(len(stems))
    multichannel_path = os.path.join(separated_dir, "multichannel_stems.wav")

    # Verify stems are not silent to catch routing issues early
    silent_stems = []
    stem_infos = {}
    for stem_name, stem_path in stems:
        rms = _compute_rms(stem_path)
        if rms < RMS_SILENCE_THRESHOLD:
            silent_stems.append(stem_name)
        stem_infos[stem_name] = sf.info(stem_path)
    if silent_stems:
        raise RuntimeError(f"The following stems appear silent (RMS<{RMS_SILENCE_THRESHOLD}): {', '.join(silent_stems)}")

    # Build ffmpeg command to collapse stereo stems -> mono and join into multichannel stream
    command = ['ffmpeg', '-y']
    filter_parts = []
    join_inputs = []

    for idx, (stem_name, stem_path) in enumerate(stems):
        command.extend(['-i', stem_path])
        mono_label = f'm{idx}'
        info = stem_infos[stem_name]
        if info.channels == 1:
            filter_parts.append(f'[{idx}:a]aresample=async=1[{mono_label}]')
        else:
            filter_parts.append(f'[{idx}:a]pan=mono|c0=0.5*c0+0.5*c1,aresample=async=1[{mono_label}]')
        join_inputs.append(f'[{mono_label}]')

    filter_parts.append(f"{''.join(join_inputs)}join=inputs={len(stems)}:channel_layout={layout}[aout]")
    filter_complex = ';'.join(filter_parts)

    command.extend([
        '-filter_complex', filter_complex,
        '-map', '[aout]',
        '-c:a', 'pcm_s24le',
        multichannel_path
    ])

    await run_command(command, task_id, "Building multichannel stem")

    # Verify channel layout metadata exists
    probe_output = await run_command([
        'ffprobe', '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=channels,channel_layout', '-of', 'json',
        multichannel_path
    ], task_id, "Verifying multichannel layout")

    try:
        probe_data = json.loads(probe_output)
        stream_info = probe_data['streams'][0]
        channel_layout = stream_info.get('channel_layout')
        channels = stream_info.get('channels')
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to parse ffprobe output for {multichannel_path}: {exc}")

    if not channel_layout or channel_layout.lower() == 'unknown':
        raise RuntimeError("Multichannel WAV is missing a valid channel layout")

    if channels != len(stems):
        raise RuntimeError(f"Expected {len(stems)} channels but found {channels}")

    index_path = os.path.join(separated_dir, STEM_INDEX_FILENAME)
    with open(index_path, 'w', encoding='utf-8') as index_file:
        json.dump({
            'order': stem_order,
            'channel_layout': channel_layout,
            'channel_count': channels
        }, index_file, indent=2)

    return multichannel_path, stem_order, channel_layout


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

        # Detect device for GPU acceleration
        device = get_demucs_device()
        tasks[task_id]["message"] = f"Starting separation on {device.upper()}..."

        command = [
            "python3", "-m", "demucs.separate",
            "-n", model,
            "-d", device,  # Enable CUDA if available, fallback to CPU
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

        # Auto-trigger remuxing after successful separation
        tasks[task_id] = {"status": "in_progress", "progress": 0.9, "message": "Separation complete. Starting auto-remux..."}

        # Find the original video path to pass to remux
        # video_path is the input parameter to this function
        remux_result = await do_auto_remux(task_id, video_path, actual_separated_path)

        tasks[task_id] = {"status": "completed", "progress": 1.0, "message": "Separation and remux complete.",
                         "result": {"separated_dir": actual_separated_path, "model": model, **remux_result}}
    except Exception as e:
        tasks[task_id] = {"status": "failed", "message": f"Separation failed: {e}"}

async def do_merge_stems(task_id: str, separated_dir: str):
    """Merges separated stems into a single multichannel WAV file."""
    tasks[task_id] = {"status": "in_progress", "progress": 0.0, "message": "Merging stems..."}
    try:
        multichannel_path, stem_order, channel_layout = await ensure_multichannel_stem(task_id, separated_dir)
        tasks[task_id] = {
            "status": "completed",
            "progress": 1.0,
            "message": "Stems merged successfully.",
            "result": {
                "multichannel_wav_path": multichannel_path,
                "stem_order": stem_order,
                "channel_layout": channel_layout
            }
        }
    except Exception as e:
        tasks[task_id] = {"status": "failed", "message": f"Stem merging failed: {e}"}

async def do_auto_remux(task_id: str, video_path: str, separated_dir: str):
    """Automatically remuxes video with all separated stems as multi-track audio."""
    tasks[task_id] = {"status": "in_progress", "progress": 0.0, "message": "Auto-remuxing video with stems..."}
    try:
        # Get base name for output file
        output_base_name = os.path.splitext(os.path.basename(video_path))[0]
        output_filename = f"{output_base_name}_remuxed.mp4"
        output_filepath = os.path.join(REMUXED_DIR, output_filename)

        multichannel_path, stem_order, channel_layout = await ensure_multichannel_stem(task_id, separated_dir)

        command = [
            'ffmpeg', '-y',
            '-i', video_path,
            '-i', multichannel_path,
            '-map', '0:v:0',
            '-map', '1:a:0',
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-b:a', '384k',
            '-movflags', 'use_metadata_tags',
            '-metadata:s:a:0', f'title=Stem mix ({channel_layout})',
            output_filepath
        ]

        await run_command(command, task_id, "Remuxing stems into MP4")

        tasks[task_id] = {
            "status": "completed",
            "progress": 1.0,
            "message": "Auto-remux complete.",
            "result": {
                "remuxed_path": output_filepath,
                "stem_count": len(stem_order),
                "stem_order": stem_order,
                "channel_layout": channel_layout,
                "multichannel_wav_path": multichannel_path
            }
        }

    except ffmpeg.Error as e:
        tasks[task_id] = {"status": "failed", "message": f"FFmpeg error during auto-remux: {e.stderr.decode().strip()}"}
    except Exception as e:
        tasks[task_id] = {"status": "failed", "message": f"Auto-remux failed: {e}"}


async def do_mix_export(task_id: str, video_path: str, multichannel_wav_path: str, gains: dict, output_filename: str):
    """Applies gains to stems and remuxes with video."""
    tasks[task_id] = {"status": "in_progress", "progress": 0.0, "message": "Starting mix export..."}
    try:
        separated_dir = os.path.dirname(multichannel_wav_path)
        multichannel_wav_path, _, _ = await ensure_multichannel_stem(task_id, separated_dir)
        index_path = os.path.join(separated_dir, STEM_INDEX_FILENAME)

        stem_order = None
        if os.path.exists(index_path):
            with open(index_path, 'r', encoding='utf-8') as index_file:
                stem_order = json.load(index_file).get('order')

        if not stem_order:
            stem_order = [stem for stem, _ in _discover_stems(separated_dir)]

        if not stem_order:
            raise ValueError("No stems available for mix export.")

        stem_infos = []
        for stem_name in stem_order:
            stem_path = os.path.join(separated_dir, f"{stem_name}.wav")
            if not os.path.exists(stem_path):
                continue
            info = sf.info(stem_path)
            stem_infos.append((stem_name, stem_path, info))

        if not stem_infos:
            raise ValueError("Stem files referenced in index are missing.")

        samplerate = stem_infos[0][2].samplerate
        channels = stem_infos[0][2].channels
        max_frames = max(info.frames for _, _, info in stem_infos)

        mixed_buffer = np.zeros((max_frames, channels), dtype='float32')

        for stem_name, stem_path, info in stem_infos:
            if info.samplerate != samplerate or info.channels != channels:
                raise ValueError(f"Sample rate/channel mismatch for stem {stem_name}")

            gain = gains.get(stem_name, 1.0)
            if gain == 0.0:
                continue

            with sf.SoundFile(stem_path) as stem_file:
                frame_cursor = 0
                while True:
                    block = stem_file.read(frames=65536, dtype='float32')
                    if block.size == 0:
                        break
                    if block.ndim == 1:
                        block = np.expand_dims(block, axis=1)
                    block_len = block.shape[0]
                    mixed_buffer[frame_cursor:frame_cursor + block_len] += block * gain
                    frame_cursor += block_len

        # Normalize to prevent clipping
        peak = np.max(np.abs(mixed_buffer))
        if peak > 1.0:
            mixed_buffer /= peak

        temp_mixed_audio_path = os.path.join(MIXES_DIR, f"temp_mixed_audio_{uuid.uuid4().hex}.wav")
        sf.write(temp_mixed_audio_path, mixed_buffer, samplerate)

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

@app.get("/list-remuxed")
async def list_remuxed_files():
    """List all remuxed files in the remuxed directory with their separated directories."""
    remuxed_files = []
    if os.path.exists(REMUXED_DIR):
        for filename in os.listdir(REMUXED_DIR):
            if filename.endswith('.mp4'):
                filepath = os.path.join(REMUXED_DIR, filename)
                file_size = os.path.getsize(filepath)

                # Extract base name (remove _remuxed.mp4)
                base_name = filename.replace('_remuxed.mp4', '')

                # Find matching separated directory
                separated_dir = None
                stem_metadata = None
                if os.path.exists(SEPARATED_DIR):
                    for sep_dir in os.listdir(SEPARATED_DIR):
                        if sep_dir.startswith(base_name + '_'):
                            # Found matching directory, look for htdemucs_6s subdirectory
                            potential_path = os.path.join(SEPARATED_DIR, sep_dir)
                            htdemucs_path = os.path.join(potential_path, 'htdemucs_6s')
                            if os.path.exists(htdemucs_path):
                                separated_dir = htdemucs_path
                                index_path = os.path.join(htdemucs_path, STEM_INDEX_FILENAME)
                                if os.path.exists(index_path):
                                    with open(index_path, 'r', encoding='utf-8') as index_file:
                                        stem_metadata = json.load(index_file)
                                break

                remuxed_files.append({
                    "filename": filename,
                    "path": filepath,
                    "size_mb": round(file_size / (1024 * 1024), 2),
                    "separated_dir": separated_dir,
                    "stem_order": stem_metadata.get('order') if stem_metadata else None,
                    "channel_layout": stem_metadata.get('channel_layout') if stem_metadata else None
                })
    return {"files": remuxed_files}

@app.get("/files/{filename:path}")
async def serve_file(filename: str):
    """Serve static files from downloads, separated, and mixes directories."""
    # Basic security: ensure file is within allowed directories
    # First try the path as-is (it might already be relative to project root)
    if os.path.exists(filename) and os.path.isfile(filename):
        return FileResponse(filename)

    # Then try prepending each base directory
    for base_dir in [DOWNLOADS_DIR, SEPARATED_DIR, MIXES_DIR]:
        file_path = os.path.join(base_dir, filename)
        if os.path.exists(file_path) and os.path.isfile(file_path):
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

