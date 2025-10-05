const { ipcRenderer } = require('electron');
const axios = require('axios');

const API_BASE_URL = 'http://localhost:8000';

// UI Elements
const youtubeUrlInput = document.getElementById('youtube-url');
const downloadBtn = document.getElementById('download-btn');
const loadRemuxedBtn = document.getElementById('load-remuxed-btn');
const remuxedFilesSelect = document.getElementById('remuxed-files');
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
let masterMixerNode = null; // Master mixer for all stems
let sourceNodes = []; // Store source nodes for playback
let isPlaying = false;
let startTime = 0;
let pauseTime = 0;
let backendStemOrder = null;
let backendChannelLayout = null;
let suppressVideoSeekHandler = false;

videoPreview.muted = true;
videoPreview.volume = 0;

videoPreview.addEventListener('loadedmetadata', () => {
  videoPreview.muted = true;
  videoPreview.volume = 0;
});

videoPreview.addEventListener('seeked', () => {
  if (!audioContext) {
    return;
  }
  if (suppressVideoSeekHandler) {
    suppressVideoSeekHandler = false;
    return;
  }
  const targetTime = videoPreview.currentTime;
  if (isPlaying) {
    restartPlaybackAt(targetTime);
  } else {
    pauseTime = targetTime;
    ipcRenderer.send('projector-seek', targetTime);
  }
});

function stopCurrentSources() {
  sourceNodes.forEach(source => {
    try {
      source.stop(0);
    } catch (err) {
      console.warn('Failed to stop source', err);
    }
  });
  sourceNodes = [];
}

function scheduleVideoSeek(timeInSeconds) {
  suppressVideoSeekHandler = true;
  videoPreview.currentTime = timeInSeconds;
}

async function startPlaybackFrom(offsetSeconds) {
  if (!audioContext) {
    return;
  }

  if (audioContext.state === 'suspended') {
    await audioContext.resume();
  }

  stopCurrentSources();

  const clampedOffset = Math.max(0, offsetSeconds);
  stemNames.forEach(stemName => {
    const buffer = audioBuffers[stemName];
    const stemGainNode = gainNodes[stemName];
    if (!buffer || !stemGainNode) {
      return;
    }

    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.channelCount = buffer.numberOfChannels;
    source.channelCountMode = 'explicit';
    source.channelInterpretation = 'speakers';
    source.connect(stemGainNode);

    const safeOffset = Math.min(clampedOffset, Math.max(0, buffer.duration - 0.05));
    try {
      source.start(0, safeOffset);
    } catch (err) {
      console.error(`Failed to start stem ${stemName} at offset ${safeOffset}`, err);
    }
    sourceNodes.push(source);
  });

  scheduleVideoSeek(clampedOffset);
  const videoPlayPromise = videoPreview.play();
  if (videoPlayPromise && typeof videoPlayPromise.catch === 'function') {
    videoPlayPromise.catch(err => console.warn('Video play rejected:', err));
  }
  ipcRenderer.send('projector-seek', clampedOffset);
  ipcRenderer.send('projector-play');

  startTime = audioContext.currentTime - clampedOffset;
  pauseTime = clampedOffset;
  isPlaying = true;
}

function restartPlaybackAt(offsetSeconds) {
  startPlaybackFrom(offsetSeconds).catch(err => console.error('Failed to restart playback', err));
}

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
        if (result.stem_order) {
          backendStemOrder = result.stem_order;
        }
        if (result.channel_layout) {
          backendChannelLayout = result.channel_layout;
        }
        if (result.multichannel_wav_path) {
          multichannelWavPath = result.multichannel_wav_path;
        }
        mergeBtn.disabled = false;

        // Display remuxed file info if available
        let alertMessage = 'Stem separation complete! You can now merge stems.';
        if (result.remuxed_path) {
          alertMessage = `Stem separation and auto-remux complete!\n\nRemuxed file: ${result.remuxed_path}\nStem count: ${result.stem_count}\n\nYou can now play and mix the stems.`;
          loadStemsForPlayback();
        }
        alert(alertMessage);
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
        if (result.stem_order) {
          backendStemOrder = result.stem_order;
        }
        if (result.channel_layout) {
          backendChannelLayout = result.channel_layout;
        }
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
    if (!separatedDir) {
      throw new Error('Separated directory is not set.');
    }

    // Clear previous state to avoid duplicate sliders
    stopCurrentSources();
    if (audioContext) {
      try {
        audioContext.close();
      } catch (closeErr) {
        console.warn('Failed to close previous AudioContext:', closeErr);
      }
    }

    stemNames = [];
    stemGains = {};
    audioBuffers = {};
    gainNodes = {};
    isPlaying = false;
    pauseTime = 0;

    audioContext = new (window.AudioContext || window.webkitAudioContext)();

    const possibleStems = backendStemOrder && backendStemOrder.length > 0
      ? backendStemOrder
      : ['vocals', 'drums', 'bass', 'guitar', 'piano', 'other'];

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

    // Create master mixer node for proper stem summing
    masterMixerNode = audioContext.createGain();
    masterMixerNode.gain.value = 1.0;
    masterMixerNode.channelCountMode = 'explicit';
    masterMixerNode.channelInterpretation = 'speakers';
  const destinationChannels = audioContext.destination.maxChannelCount || 2;
  masterMixerNode.channelCount = Math.min(destinationChannels, 2);
    masterMixerNode.connect(audioContext.destination);
    console.log('Created master mixer node');

    // Create gain nodes for each stem with explicit stereo configuration
    stemNames.forEach(stemName => {
      const gainNode = audioContext.createGain();
      gainNode.gain.value = stemGains[stemName];

      const buffer = audioBuffers[stemName];
      gainNode.channelCount = buffer ? buffer.numberOfChannels : 2;
      gainNode.channelCountMode = 'explicit';
      gainNode.channelInterpretation = 'speakers';

      // Connect to master mixer instead of directly to destination
      gainNode.connect(masterMixerNode);
      gainNodes[stemName] = gainNode;

      console.log(`Created gain node for ${stemName}: channels=${gainNode.channelCount}, gain=${gainNode.gain.value}`);
    });

    // Generate sliders
    generateStemSliders();

    // Completely disable video audio - use Web Audio API for playback
    videoPreview.muted = true;
    videoPreview.volume = 0;
    console.log('Video audio disabled');

    // Enable playback controls
    playBtn.disabled = false;
    pauseBtn.disabled = false;
    stopBtn.disabled = false;
    projectorBtn.disabled = false;
    exportBtn.disabled = false;

    if (backendChannelLayout) {
      console.log(`Loaded ${stemNames.length} stems with layout ${backendChannelLayout}`);
    } else {
      console.log(`Loaded ${stemNames.length} stems successfully`);
    }

  } catch (error) {
    console.error('Failed to load stems:', error);
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
    return;
  }

  startPlaybackFrom(pauseTime)
    .then(() => console.log('Playback started successfully'))
    .catch(err => alert(`Unable to start playback: ${err.message}`));
});

pauseBtn.addEventListener('click', () => {
  if (!isPlaying) {
    return;
  }

  stopCurrentSources();
  videoPreview.pause();
  ipcRenderer.send('projector-pause');

  pauseTime = audioContext.currentTime - startTime;
  isPlaying = false;
});

stopBtn.addEventListener('click', () => {
  stopCurrentSources();
  videoPreview.pause();
  scheduleVideoSeek(0);
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

// --- Load Remuxed Files ---
let remuxedFilesData = []; // Store file data including separated_dir

async function loadRemuxedFilesList() {
  try {
    const response = await axios.get(`${API_BASE_URL}/list-remuxed`);
    const { files } = response.data;

    remuxedFilesData = files; // Store for later use

    // Clear existing options except the first one
    remuxedFilesSelect.innerHTML = '<option value="">-- Select a remuxed file --</option>';

    // Populate dropdown
    files.forEach((file, index) => {
      const option = document.createElement('option');
      option.value = index; // Use index to look up in remuxedFilesData
      option.textContent = `${file.filename} (${file.size_mb} MB)`;
      remuxedFilesSelect.appendChild(option);
    });
  } catch (error) {
    console.error('Failed to load remuxed files:', error);
  }
}

// Load remuxed file button handler
loadRemuxedBtn.addEventListener('click', async () => {
  const selectedIndex = remuxedFilesSelect.value;
  if (!selectedIndex) {
    alert('Please select a remuxed file first');
    return;
  }

  const fileData = remuxedFilesData[selectedIndex];
  if (!fileData) {
    alert('File data not found');
    return;
  }

  // Set the video path and load it
  downloadedVideoPath = fileData.path;
  const normalizedVideoPath = fileData.path.replace(/^\.\//, '');
  videoPreview.src = `${API_BASE_URL}/files/${normalizedVideoPath}`;

  // Check if separated directory is available
  if (fileData.separated_dir) {
    separatedDir = fileData.separated_dir;
    backendStemOrder = Array.isArray(fileData.stem_order) ? fileData.stem_order : backendStemOrder;
    backendChannelLayout = fileData.channel_layout || backendChannelLayout;

    // Load stems for playback
    await loadStemsForPlayback();

    // Enable playback controls
    playBtn.disabled = false;
    pauseBtn.disabled = false;
    stopBtn.disabled = false;
    projectorBtn.disabled = false;

    alert(`Loaded: ${fileData.filename}\n\nStems loaded and ready for playback!`);
  } else {
    alert(`Loaded: ${fileData.filename}\n\nWarning: Separated stems not found. Cannot enable mixing.`);
  }
});

// Load remuxed files list on page load
window.addEventListener('DOMContentLoaded', () => {
  loadRemuxedFilesList();
});
