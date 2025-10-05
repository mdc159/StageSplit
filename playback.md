Totally fixable. In practice this “only bass + sliders do nothing” comes from one of two places:

1. **The multichannel file is wrong or has no channel layout**, so playback/downmix picks one channel (often bass) and ignores the rest.
2. **Your preview path isn’t using your mixer at all** (e.g., you’re hearing the video element’s built-in audio, or your WebAudio graph isn’t wired).

Here’s a tight triage + fix plan.

---

## 5-minute triage

**A. Prove what you’re actually hearing**

* Temporarily **mute the `<video>` element**: `video.muted = true`.

  * If you still hear bass, you’re not hearing the video track—you’re hearing your own audio path (but it’s only one channel).
  * If audio disappears, your preview was coming from the video’s original track (sliders won’t affect that).

**B. Inspect the multichannel WAV**

```bash
ffprobe -v error -select_streams a:0 -show_entries stream=channels,channel_layout -of default=nk=1:nw=1 stems_multichannel.wav
```

Expect `channels=4` (or 6) and a real layout (e.g., `4.0`, `5.1`, `6.0`). If layout is empty/unknown, many players pick a single channel.

**C. Extract each channel to check content**

```bash
# Extract channel i (0-based)
ffmpeg -y -i stems_multichannel.wav -map_channel 0.0.0 ch1.wav
ffmpeg -y -i stems_multichannel.wav -map_channel 0.0.1 ch2.wav
ffmpeg -y -i stems_multichannel.wav -map_channel 0.0.2 ch3.wav
ffmpeg -y -i stems_multichannel.wav -map_channel 0.0.3 ch4.wav
# (add .4, .5 if you have 6)
```

Listen: are all stems present?

---

## Common root causes → fixes

### 1) Merge step (FFmpeg) created an “odd” stream

Using `amerge` without a layout often yields a multichannel stream with **no layout**, and downstream players may pick one channel. Prefer **`join`** with an explicit layout.

**4 stems → 4-channel WAV**

```bash
ffmpeg -y -i vocals.wav -i drums.wav -i bass.wav -i other.wav \
  -filter_complex "join=inputs=4:channel_layout=4.0" \
  stems_multichannel.wav
```

**6 stems → 6-channel WAV (generic)**

```bash
ffmpeg -y -i vocals.wav -i drums.wav -i bass.wav -i guitar.wav -i piano.wav -i other.wav \
  -filter_complex "join=inputs=6:channel_layout=6.0" \
  stems_multichannel.wav
```

*(You can also map to `5.1` if you want broad player support, but then you must decide which stem becomes LFE, Center, etc.)*

### 2) Preview path is using the wrong audio

In Electron, don’t rely on the `<video>` element’s audio. **Mute the video** and drive audio via **Web Audio API** from your multichannel WAV so sliders actually affect gain.

Minimal renderer sketch:

```js
const ctx = new AudioContext();

async function loadAndWire(url, gainsArray) {
  const res = await fetch(url);
  const buf = await res.arrayBuffer();
  const audioBuf = await ctx.decodeAudioData(buf); // multichannel

  const src = ctx.createBufferSource();
  src.buffer = audioBuf;

  const split = ctx.createChannelSplitter(audioBuf.numberOfChannels);
  const merger = ctx.createChannelMerger(2);

  const gainNodes = [];
  for (let i = 0; i < audioBuf.numberOfChannels; i++) {
    const g = ctx.createGain();
    g.gain.value = gainsArray?.[i] ?? 1.0;
    split.connect(g, i);
    // simple mono-to-stereo: send equally to L and R
    g.connect(merger, 0, 0);
    g.connect(merger, 0, 1);
    gainNodes.push(g);
  }

  src.connect(split);
  merger.connect(ctx.destination);

  return { src, gainNodes };
}

// Usage:
// video.muted = true;
// const { src, gainNodes } = await loadAndWire('stems_multichannel.wav');
// src.start(0, video.currentTime); // crude sync
// slider onChange -> gainNodes[i].gain.value = newValue;
```

On seek: stop the source and **restart at `video.currentTime`**; for long sessions, periodically check drift and restart if >50 ms.

### 3) Export mapping mixed only one channel

If your export uses FFmpeg, ensure you **split → apply gains → mix** explicitly.

**Example (6 channels → stereo with per-channel gains):**

```bash
ffmpeg -y -i stems_multichannel.wav -i video.mp4 -filter_complex "\
[0:a]channelsplit=channel_layout=6c[c0][c1][c2][c3][c4][c5]; \
[c0]volume=g0[a0]; [c1]volume=g1[a1]; [c2]volume=g2[a2]; \
[c3]volume=g3[a3]; [c4]volume=g4[a4]; [c5]volume=g5[a5]; \
[a0][a1][a2][a3][a4][a5]amix=inputs=6:normalize=0, \
dynaudnorm[outa]" \
-map 1:v:0 -map "[outa]" -c:v copy -shortest remixed.mp4
```

Replace `g0..g5` with your slider values (e.g., `0.0–2.0`).
If you want **multichannel export**, replace the `amix` part with a **`join`** using per-channel `volume` first.

---

## Quick “is it the player?” sanity check

Try forcing a temporary stereo preview from the multi-WAV:

```bash
ffmpeg -y -i stems_multichannel.wav \
  -af "pan=stereo|FL=c0+c2|FR=c1+c3" \
  test_preview.wav
```

If *this* plays fine in your Electron `<audio>`/`<video>` preview, the player is okay and your multichannel or wiring was the issue.

---

## Typical gotchas checklist

* The video element wasn’t muted → you were hearing the original track.
* The merge used `amerge` without setting a layout → downstream picked one channel.
* WebAudio graph didn’t connect gains to the destination (one channel wired).
* Export `filter_complex` mapped only `c2` (bass) into both L/R by mistake.
* System output set to **mono** in OS accessibility (rare, but check).
