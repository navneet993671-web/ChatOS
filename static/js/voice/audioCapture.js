// static/js/voice/audioCapture.js
//
// Microphone capture for push-to-talk.
//
// Responsibilities, and nothing else:
//   * ask for permission and report *why* it failed
//   * record to a single webm blob
//   * enforce a maximum duration so the mic can never be left open forever
//   * expose a coarse level meter so the overlay can show a waveform
//
// It does not transcribe, does not touch the network, and does not know about
// the chat. That keeps it testable (see tests/test_voice_frontend.py) and keeps
// the retry/abort logic in one place, in voiceAssistant.js.

import {
  VOICE_ERRORS,
  VOICE_DEFAULTS,
  normalizeMicError,
} from './voiceTypes.js';

/** Browsers that can record. */
export function isSupported(win = globalThis.window) {
  if (!win) return false;
  const md = win.navigator && win.navigator.mediaDevices;
  return !!(md && typeof md.getUserMedia === 'function' && win.MediaRecorder);
}

/** Prefer opus/webm; fall back to whatever the browser will take. */
function pickMimeType(win) {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/ogg;codecs=opus',
  ];
  if (!win.MediaRecorder || typeof win.MediaRecorder.isTypeSupported !== 'function') {
    return '';
  }
  for (const type of candidates) {
    try {
      if (win.MediaRecorder.isTypeSupported(type)) return type;
    } catch (e) {
      /* keep looking */
    }
  }
  return '';
}

/** Browser-local (no server round trip) cause, checked before getUserMedia. */
export function preflight(win = globalThis.window) {
  if (!win) return VOICE_ERRORS.UNSUPPORTED;
  if (win.isSecureContext === false) return VOICE_ERRORS.INSECURE_CONTEXT;
  if (!isSupported(win)) return VOICE_ERRORS.UNSUPPORTED;
  return null;
}

/**
 * Create a capture session.
 *
 * @param {object} [options]
 * @param {number} [options.maxDurationMs]  hard stop, mirrors
 *   VOICE_MAX_RECORDING_SECONDS on the server.
 * @param {string} [options.deviceId]      '' means the system default.
 * @param {function} [options.onLevel]     0..1, called while recording.
 * @param {function} [options.onAutoStop]  fired when the max duration hits.
 * @param {object} [options.win]           injectable window (tests).
 */
export function createCapture(options = {}) {
  const win = options.win || globalThis.window;
  const maxDurationMs =
    options.maxDurationMs != null
      ? Number(options.maxDurationMs)
      : VOICE_DEFAULTS.maxRecordingSeconds * 1000;

  let recorder = null;
  let stream = null;
  let chunks = [];
  let startedAt = 0;
  let maxTimer = null;
  let levelRaf = null;
  let audioCtx = null;
  let analyser = null;
  let stopped = false;
  let autoStopped = false;

  // ── level meter, best effort ─────────────────────────────────────────
  function startLevelMeter(activeStream) {
    if (typeof options.onLevel !== 'function') return;
    const Ctx = win.AudioContext || win.webkitAudioContext;
    if (!Ctx) return;
    try {
      audioCtx = new Ctx();
      const source = audioCtx.createMediaStreamSource(activeStream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      const buf = new Uint8Array(analyser.frequencyBinCount);

      const tick = () => {
        if (stopped || !analyser) return;
        analyser.getByteTimeDomainData(buf);
        // Root-mean-square deviation from the 128 midpoint.
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        const level = Math.min(1, Math.sqrt(sum / buf.length) * 3);
        try {
          options.onLevel(level);
        } catch (e) {
          /* a UI callback must never break capture */
        }
        levelRaf = win.requestAnimationFrame
          ? win.requestAnimationFrame(tick)
          : win.setTimeout(tick, 60);
      };
      tick();
    } catch (e) {
      // A missing AudioContext is not a reason to refuse to record.
      audioCtx = null;
      analyser = null;
    }
  }

  function stopLevelMeter() {
    if (levelRaf != null) {
      if (win.cancelAnimationFrame) win.cancelAnimationFrame(levelRaf);
      else win.clearTimeout(levelRaf);
      levelRaf = null;
    }
    analyser = null;
    if (audioCtx) {
      try {
        audioCtx.close();
      } catch (e) {
        /* already closed */
      }
      audioCtx = null;
    }
  }

  function releaseTracks() {
    if (stream) {
      try {
        stream.getTracks().forEach((t) => t.stop());
      } catch (e) {
        /* nothing we can do */
      }
      stream = null;
    }
  }

  function cleanup() {
    stopped = true;
    if (maxTimer) {
      win.clearTimeout(maxTimer);
      maxTimer = null;
    }
    stopLevelMeter();
    releaseTracks();
  }

  /**
   * Begin recording. Resolves once the recorder is actually running, so the
   * caller can trust that "listening" means the mic is hot.
   */
  async function start() {
    const blocked = preflight(win);
    if (blocked) {
      const err = new Error(blocked);
      err.code = blocked;
      throw err;
    }

    stopped = false;
    autoStopped = false;
    chunks = [];

    const constraints = { audio: true };
    if (options.deviceId) {
      constraints.audio = {
        deviceId: { exact: options.deviceId },
        // Room for the browser to fall back rather than hard-fail.
        echoCancellation: true,
        noiseSuppression: true,
      };
    }

    try {
      stream = await win.navigator.mediaDevices.getUserMedia(constraints);
    } catch (e) {
      const code = normalizeMicError(e);
      const err = new Error(code);
      err.code = code;
      err.cause = e;
      throw err;
    }

    const mimeType = pickMimeType(win);
    try {
      recorder = mimeType
        ? new win.MediaRecorder(stream, { mimeType })
        : new win.MediaRecorder(stream);
    } catch (e) {
      cleanup();
      const err = new Error(VOICE_ERRORS.INIT_FAILED);
      err.code = VOICE_ERRORS.INIT_FAILED;
      err.cause = e;
      throw err;
    }

    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) chunks.push(event.data);
    };

    // Timeslice keeps chunks flowing instead of one big blob at the end.
    try {
      recorder.start(250);
    } catch (e) {
      recorder.start();
    }

    startedAt = Date.now();
    startLevelMeter(stream);

    // The guard the spec calls out: never record forever.
    maxTimer = win.setTimeout(() => {
      autoStopped = true;
      if (typeof options.onAutoStop === 'function') {
        try {
          options.onAutoStop();
        } catch (e) {
          /* ignore */
        }
      }
    }, maxDurationMs);

    return { maxDurationMs, mimeType };
  }

  /**
   * Stop and resolve with the recording.
   *
   * Resolves with `{ blob, durationMs, autoStopped }`; an empty blob is
   * returned as-is and the caller reports `empty_transcript` rather than
   * inventing audio.
   */
  function stop() {
    return new Promise((resolve) => {
      if (!recorder || recorder.state === 'inactive') {
        const durationMs = startedAt ? Date.now() - startedAt : 0;
        cleanup();
        resolve({ blob: null, durationMs, autoStopped });
        return;
      }

      const durationMs = startedAt ? Date.now() - startedAt : 0;
      const wasAuto = autoStopped;

      recorder.onstop = () => {
        const type = recorder.mimeType || 'audio/webm';
        const blob = chunks.length ? new Blob(chunks, { type }) : null;
        cleanup();
        resolve({ blob, durationMs, autoStopped: wasAuto });
      };

      try {
        recorder.stop();
      } catch (e) {
        cleanup();
        resolve({ blob: null, durationMs, autoStopped: wasAuto });
      }
    });
  }

  /** Drop the recording without producing audio (Escape / barge-in). */
  function cancel() {
    try {
      if (recorder && recorder.state !== 'inactive') {
        recorder.onstop = null;
        recorder.stop();
      }
    } catch (e) {
      /* ignore */
    }
    chunks = [];
    cleanup();
  }

  return {
    start,
    stop,
    cancel,
    get isRecording() {
      return !!(recorder && recorder.state === 'recording');
    },
    get durationMs() {
      return startedAt ? Date.now() - startedAt : 0;
    },
  };
}

/**
 * List microphones for the settings panel.
 *
 * Labels are only populated after permission has been granted at least once,
 * so an empty label is normal on first run and is surfaced as "Microphone N".
 */
export async function listInputDevices(win = globalThis.window) {
  if (!win || !win.navigator || !win.navigator.mediaDevices) return [];
  if (typeof win.navigator.mediaDevices.enumerateDevices !== 'function') return [];
  try {
    const devices = await win.navigator.mediaDevices.enumerateDevices();
    return devices
      .filter((d) => d.kind === 'audioinput')
      .map((d, i) => ({
        deviceId: d.deviceId,
        label: d.label || `Microphone ${i + 1}`,
        isDefault: d.deviceId === 'default',
      }));
  } catch (e) {
    return [];
  }
}
