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
