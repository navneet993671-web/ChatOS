// static/js/video/browserEncoder.js
//
// The fallback half of the hybrid engine: when the server has no ffmpeg, the
// browser encodes the edit itself.
//
// How it works — video element → canvas → MediaRecorder:
//   * the source plays, muted to the speakers but tapped by WebAudio for audio;
//   * every presented frame is drawn to a canvas with the crop/rotate/flip
//     applied, so the recorded stream *is* the edit;
//   * `playbackRate` changes the timeline, and a GainNode applies volume;
//   * MediaRecorder writes WebM from the canvas + audio tracks.
//
// The honest cost: because it captures in real time, a one-minute clip takes
// about a minute. The UI says so rather than pretending otherwise. Output is
// always WebM — MediaRecorder cannot produce MP4 or GIF, which is exactly why
// the server path exists.

import { progressFromPosition } from './ops.js';

/** MIME candidates in order of preference; VP9 is smaller, VP8 is universal. */
const MIME_CANDIDATES = [
  'video/webm;codecs=vp9,opus',
  'video/webm;codecs=vp8,opus',
  'video/webm;codecs=vp9',
  'video/webm;codecs=vp8',
  'video/webm',
];

export class BrowserEncodeError extends Error {
  constructor(message, code = 'browser_encode_failed') {
    super(message);
    this.name = 'BrowserEncodeError';
    this.code = code;
  }
}

/** Whether this browser can encode at all. */
export function isSupported() {
  return (
    typeof MediaRecorder !== 'undefined' &&
    typeof HTMLCanvasElement !== 'undefined' &&
    typeof HTMLCanvasElement.prototype.captureStream === 'function' &&
    typeof document !== 'undefined'
  );
}

/** The first WebM codec combination this browser actually accepts. */
export function pickMimeType() {
  if (typeof MediaRecorder === 'undefined') return null;
  if (typeof MediaRecorder.isTypeSupported !== 'function') return 'video/webm';
  for (const candidate of MIME_CANDIDATES) {
    if (MediaRecorder.isTypeSupported(candidate)) return candidate;
  }
  return null;
}

function even(value) {
  const n = Math.max(2, Math.round(value));
  return n % 2 === 0 ? n : n - 1;
}

/** Output dimensions for the edit, as the encoder will produce them. */
export function outputDimensions(sourceWidth, sourceHeight, ops) {
  const crop = ops.crop;
  const cropW = crop ? sourceWidth * crop.width : sourceWidth;
  const cropH = crop ? sourceHeight * crop.height : sourceHeight;
  const swapped = ops.rotate === 90 || ops.rotate === 270;
  return {
    width: even(swapped ? cropH : cropW),
    height: even(swapped ? cropW : cropH),
  };
}

function waitForEvent(target, event, timeoutMs = 20000, signal = null) {
  return new Promise((resolve, reject) => {
    let timer = null;
    const cleanup = () => {
      target.removeEventListener(event, onEvent);
      target.removeEventListener('error', onError);
      if (timer) clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
    };
    const onEvent = () => { cleanup(); resolve(); };
    const onError = () => { cleanup(); reject(new BrowserEncodeError(`Video failed to load (${event})`, 'media_error')); };
    const onAbort = () => { cleanup(); reject(abortError()); };
    target.addEventListener(event, onEvent, { once: true });
    target.addEventListener('error', onError, { once: true });
    if (signal) {
      if (signal.aborted) { cleanup(); reject(abortError()); return; }
      signal.addEventListener('abort', onAbort, { once: true });
    }
    timer = setTimeout(() => {
      cleanup();
      reject(new BrowserEncodeError('Timed out waiting for the video to be ready', 'media_timeout'));
    }, timeoutMs);
  });
}

function abortError() {
  const err = new Error('Export cancelled');
  err.name = 'AbortError';
  return err;
}

/**
 * Encode `ops` for the video at `url`, in the browser.
 *
 * @returns {Promise<{blob: Blob, ext: string, mimeType: string, width: number,
 *                    height: number, durationSeconds: number}>}
 */
export async function encodeLocally({ url, ops, duration = 0, onProgress, signal = null }) {
  if (!isSupported()) {
    throw new BrowserEncodeError(
      'This browser cannot encode video. Install ffmpeg on the server to export here.',
      'unsupported'
    );
  }
  const mimeType = pickMimeType();
  if (!mimeType) {
    throw new BrowserEncodeError(
      'This browser has no WebM encoder. Install ffmpeg on the server to export here.',
      'unsupported'
    );
  }
  if (signal?.aborted) throw abortError();

  const video = document.createElement('video');
  video.src = url;
  video.playsInline = true;
  video.preload = 'auto';
  // Audio must stay in the media graph for WebAudio to tap it, so the element
  // is not muted. Silence is achieved by never routing the graph to the
  // speakers — see the `ac.destination` note below.
  video.volume = 1;

  let audioContext = null;
  let recorder = null;
  let frameHandle = null;
  let usingVideoFrameCallback = false;
  let finished = false;

  const range = {
    start: Math.max(0, ops.trimStart || 0),
    end: ops.trimEnd === null || ops.trimEnd === undefined
      ? (duration > 0 ? duration : 0)
      : ops.trimEnd,
  };
  range.length = Math.max(0.001, range.end - range.start);

  const teardown = () => {
    try { video.pause(); } catch { /* already paused */ }
    if (frameHandle !== null) {
      if (usingVideoFrameCallback && video.cancelVideoFrameCallback) {
        video.cancelVideoFrameCallback(frameHandle);
      } else {
        cancelAnimationFrame(frameHandle);
      }
      frameHandle = null;
    }
    try { video.removeAttribute('src'); video.load(); } catch { /* ignore */ }
    if (audioContext && audioContext.state !== 'closed') {
      audioContext.close().catch(() => {});
    }
  };

  try {
    await waitForEvent(video, 'loadedmetadata', 20000, signal);
    if (!video.videoWidth || !video.videoHeight) {
      throw new BrowserEncodeError('This video has no decodable picture track', 'media_error');
    }
    if (!duration && Number.isFinite(video.duration)) {
      range.end = video.duration;
      range.length = Math.max(0.001, range.end - range.start);
    }

    const dims = outputDimensions(video.videoWidth, video.videoHeight, ops);

    // Stage holds the cropped source region at native resolution; the output
    // canvas holds it rotated. Two canvases keep the per-frame maths trivial.
    const stage = document.createElement('canvas');
    const cropW = Math.round(ops.crop ? video.videoWidth * ops.crop.width : video.videoWidth);
    const cropH = Math.round(ops.crop ? video.videoHeight * ops.crop.height : video.videoHeight);
    stage.width = Math.max(2, cropW);
    stage.height = Math.max(2, cropH);
    const stageCtx = stage.getContext('2d');

    const canvas = document.createElement('canvas');
    canvas.width = dims.width;
    canvas.height = dims.height;
    const ctx = canvas.getContext('2d');

    const stream = canvas.captureStream(0);

    // Audio. `createMediaElementSource` takes the element out of the default
    // output path entirely, so the only destination is the recording — the
    // user hears nothing while it encodes, which is what we want.
    let gain = null;
    if (!ops.mute) {
      const AudioCtor = window.AudioContext || window.webkitAudioContext;
      if (AudioCtor) {
        try {
          audioContext = new AudioCtor();
          const source = audioContext.createMediaElementSource(video);
          gain = audioContext.createGain();
          gain.gain.value = Math.max(0, ops.volume);
          const destination = audioContext.createMediaStreamDestination();
          source.connect(gain);
          gain.connect(destination);
          // NOTE: deliberately not connected to audioContext.destination.
          const [track] = destination.stream.getAudioTracks();
          if (track) stream.addTrack(track);
        } catch {
          // Audio is best-effort: a silent export beats a failed one.
          audioContext = null;
          gain = null;
        }
      }
    }

    const drawFrame = () => {
      if (finished) return;
      try {
        if (gain) gain.gain.value = Math.max(0, ops.volume);
        const sx = ops.crop ? video.videoWidth * ops.crop.x : 0;
        const sy = ops.crop ? video.videoHeight * ops.crop.y : 0;
        stageCtx.drawImage(video, sx, sy, cropW, cropH, 0, 0, stage.width, stage.height);

        // Clear, then draw the stage with the rotation/flip baked in. The
        // transform is applied about the centre so the rotated bounding box
        // matches the swapped canvas dimensions exactly.
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.translate(canvas.width / 2, canvas.height / 2);
        ctx.rotate((ops.rotate || 0) * Math.PI / 180);
        ctx.scale(ops.flipH ? -1 : 1, ops.flipV ? -1 : 1);
        ctx.drawImage(stage, -stage.width / 2, -stage.height / 2, stage.width, stage.height);
        ctx.restore();
      } catch {
        // A torn frame mid-seek is not worth aborting the export over.
      }
      // captureStream(0) requires an explicit request for each frame.
      const [track] = stream.getVideoTracks();
      if (track && typeof track.requestFrame === 'function') track.requestFrame();
    };

    const onTick = () => {
      if (finished) return;
      drawFrame();
      if (onProgress) onProgress(progressFromPosition(video.currentTime, range));
      if (video.currentTime >= range.end || video.ended) {
        finish();
        return;
      }
      scheduleNextFrame();
    };

    const scheduleNextFrame = () => {
      if (finished) return;
      if (typeof video.requestVideoFrameCallback === 'function') {
        usingVideoFrameCallback = true;
        frameHandle = video.requestVideoFrameCallback(onTick);
      } else {
        usingVideoFrameCallback = false;
        frameHandle = requestAnimationFrame(onTick);
      }
    };

    const stopReason = { error: null };

    const finish = (error = null) => {
      if (finished) return;
      finished = true;
      stopReason.error = error;
      if (frameHandle !== null) {
        if (usingVideoFrameCallback && video.cancelVideoFrameCallback) {
          video.cancelVideoFrameCallback(frameHandle);
        } else {
          cancelAnimationFrame(frameHandle);
        }
        frameHandle = null;
      }
      try { video.pause(); } catch { /* ignore */ }
      if (recorder && recorder.state !== 'inactive') {
        recorder.stop();
      } else {
        teardown();
      }
    };

    const chunks = [];
    const finishedRecording = new Promise((resolve, reject) => {
      recorder = new MediaRecorder(stream, {
        mimeType,
        videoBitsPerSecond: Math.min(
          20000000,
          Math.max(1000000, Math.round(dims.width * dims.height * 3))
        ),
      });
      recorder.ondataavailable = (event) => {
        if (event.data && event.data.size) chunks.push(event.data);
      };
      recorder.onerror = () => {
        finished = true;
        teardown();
        reject(new BrowserEncodeError('The browser encoder failed', 'browser_encode_failed'));
      };
      recorder.onstop = () => {
        teardown();
        if (stopReason.error) {
          reject(stopReason.error);
          return;
        }
        const blob = new Blob(chunks, { type: mimeType.split(';')[0] || 'video/webm' });
        if (!blob.size) {
          reject(new BrowserEncodeError('The browser produced an empty file', 'empty_output'));
          return;
        }
        if (onProgress) onProgress(1);
        resolve({
          blob,
          ext: 'webm',
          mimeType,
          width: dims.width,
          height: dims.height,
          durationSeconds: range.length,
        });
      };
    });

    if (signal) {
      signal.addEventListener('abort', () => finish(abortError()), { once: true });
    }

    // Seek to the start of the kept range before recording, so the export
    // begins on the requested frame rather than on frame zero.
    if (range.start > 0) {
      const seeked = waitForEvent(video, 'seeked', 20000, signal);
      video.currentTime = range.start;
      await seeked;
    }

    video.playbackRate = Math.min(4, Math.max(0.25, ops.speed || 1));
    await video.play();
    if (audioContext && audioContext.state === 'suspended') {
      // Autoplay policy can suspend the context; resume inside the user gesture.
      await audioContext.resume().catch(() => {});
    }

    recorder.start(1000);
    scheduleNextFrame();

    return await finishedRecording;
  } catch (error) {
    finished = true;
    teardown();
    if (error?.name === 'AbortError') throw error;
    if (error instanceof BrowserEncodeError) throw error;
    throw new BrowserEncodeError(error?.message || 'Browser export failed');
  }
}
