// static/js/voice/voiceTypes.js
//
// Voice vocabulary shared by the capture, playback and controller modules.
//
// The state names mirror `services/voice/vad.py` on the backend so a server
// voice event and a client voice event describe the same thing with the same
// word. Keep the two lists in sync.
//
// This module is deliberately free of DOM and network access so it can be
// unit-tested under `node --input-type=module` (see tests/test_voice_frontend.py).

/** States a voice interaction can be in. Mirrors VOICE_STATUSES in Python. */
export const VOICE_STATES = Object.freeze({
  IDLE: 'idle',
  LISTENING: 'listening',
  SPEECH_DETECTED: 'speech_detected',
  PROCESSING: 'processing',
  SPEAKING: 'speaking',
  INTERRUPTED: 'interrupted',
  ERROR: 'error',
});

/** Human-facing labels. Kept here so the overlay and the tests agree. */
export const VOICE_STATE_LABELS = Object.freeze({
  idle: 'Ready',
  listening: 'Listening\u2026',
  speech_detected: 'Hearing you\u2026',
  processing: 'Thinking\u2026',
  speaking: 'Speaking\u2026',
  interrupted: 'Interrupted',
  error: 'Voice error',
});

/**
 * Machine-readable failure codes. The UI maps these to sentences and to a
 * recovery action; it never shows a raw provider exception.
 */
export const VOICE_ERRORS = Object.freeze({
  INSECURE_CONTEXT: 'insecure_context',
  UNSUPPORTED: 'unsupported',
  PERMISSION_DENIED: 'permission_denied',
  NO_MICROPHONE: 'no_microphone',
  DEVICE_BUSY: 'device_busy',
  INIT_FAILED: 'init_failed',
  RECORDING_TOO_LONG: 'recording_too_long',
  EMPTY_TRANSCRIPT: 'empty_transcript',
  STT_UNAVAILABLE: 'stt_unavailable',
  TTS_UNAVAILABLE: 'tts_unavailable',
  PROVIDER_UNAVAILABLE: 'provider_unavailable',
  BACKEND_UNAVAILABLE: 'backend_unavailable',
  NOT_AUTHENTICATED: 'not_authenticated',
  DISABLED: 'disabled',
  ABORTED: 'aborted',
  UNKNOWN: 'unknown',
});

/** Copy shown next to each error code. Never relies on colour alone. */
export const VOICE_ERROR_MESSAGES = Object.freeze({
  insecure_context:
    'The microphone needs a secure context. Open Misantropic over HTTPS or on localhost.',
  unsupported: 'This browser cannot record audio.',
  permission_denied:
    'Microphone access was blocked. Allow it in your browser\u2019s site settings, then try again.',
  no_microphone: 'No microphone was found. Connect one and try again.',
  device_busy: 'The microphone is in use by another app. Close it and try again.',
  init_failed: 'The microphone could not be started.',
  recording_too_long: 'Recording stopped \u2014 it reached the maximum length.',
  empty_transcript: 'Nothing was heard. Try again and speak a little closer to the mic.',
  stt_unavailable:
    'Speech-to-text is not configured on the server. Enable it in Settings, or use the browser engine.',
  tts_unavailable: 'Text-to-speech is not configured, so replies cannot be spoken yet.',
  provider_unavailable: 'The configured voice provider is not available on this server.',
  backend_unavailable: 'The Misantropic backend could not be reached.',
  not_authenticated: 'Sign in to use voice mode.',
  disabled: 'Voice is disabled on this server (VOICE_ENABLED=false).',
  aborted: 'Cancelled.',
  unknown: 'Something went wrong with voice input.',
});

/** Which states mean "the mic is open and we are capturing". */
export const CAPTURING_STATES = Object.freeze([
  VOICE_STATES.LISTENING,
  VOICE_STATES.SPEECH_DETECTED,
]);

/**
 * Which transitions are legal.
 *
 * Recording-forever is the failure mode this guards against: every path back
 * to `idle` is explicit, and `error` is reachable from anywhere so a provider
 * failure can never strand the UI in `listening`.
 */
const TRANSITIONS = Object.freeze({
  idle: ['listening', 'processing', 'error'],
  listening: ['speech_detected', 'processing', 'idle', 'interrupted', 'error'],
  speech_detected: ['listening', 'processing', 'idle', 'interrupted', 'error'],
  processing: ['speaking', 'listening', 'idle', 'interrupted', 'error'],
  speaking: ['idle', 'listening', 'interrupted', 'error'],
  interrupted: ['listening', 'idle', 'processing', 'error'],
  error: ['idle', 'listening'],
});

/**
 * Is `to` a legal move from `from`?
 *
 * Unknown states are treated as illegal rather than throwing, because this is
 * driven by server events and a typo there must not take the Chat page down.
 */
export function canTransition(from, to) {
  const allowed = TRANSITIONS[from];
  if (!allowed) return false;
  return allowed.indexOf(to) !== -1;
}

/**
 * A tiny state holder that only ever moves along legal edges.
 *
 * `force` exists for the one case where the client knows better than the
 * table: a server `voice.*` event arriving after a local abort.
 */
export function createVoiceStateMachine(initial = VOICE_STATES.IDLE, onChange = null) {
  let current = initial;

  function set(next, { force = false } = {}) {
    if (next === current) return current;
    if (!force && !canTransition(current, next)) {
      // Illegal moves are dropped, not thrown: the overlay must stay usable.
      return current;
    }
    const previous = current;
    current = next;
    if (typeof onChange === 'function') onChange(next, previous);
    return current;
  }

  return {
    get state() {
      return current;
    },
    set,
    isCapturing() {
      return CAPTURING_STATES.indexOf(current) !== -1;
    },
    reset() {
      return set(VOICE_STATES.IDLE, { force: true });
    },
  };
}

/** Default timing. The server can override these via /api/voice/settings. */
export const VOICE_DEFAULTS = Object.freeze({
  silenceTimeoutMs: 1200,
  maxRecordingSeconds: 120,
  // Hold longer than this and releasing the button sends immediately
  // (true push-to-talk); a shorter tap latches recording on/off instead.
  tapHoldThresholdMs: 350,
});

/**
 * Normalise a browser microphone failure into one of VOICE_ERRORS.
 *
 * getUserMedia rejects with a DOMException whose `name` is the only reliable
 * signal, so that is all this reads.
 */
export function normalizeMicError(error) {
  const name = (error && error.name) || '';
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return VOICE_ERRORS.PERMISSION_DENIED;
    case 'NotFoundError':
    case 'DevicesNotFoundError':
      return VOICE_ERRORS.NO_MICROPHONE;
    case 'NotReadableError':
    case 'TrackStartError':
      return VOICE_ERRORS.DEVICE_BUSY;
    case 'OverconstrainedError':
      return VOICE_ERRORS.NO_MICROPHONE;
    case 'AbortError':
      return VOICE_ERRORS.ABORTED;
    default:
      return VOICE_ERRORS.INIT_FAILED;
  }
}

/** Sentence for an error code, falling back to the generic message. */
export function errorMessage(code) {
  return VOICE_ERROR_MESSAGES[code] || VOICE_ERROR_MESSAGES.unknown;
}
