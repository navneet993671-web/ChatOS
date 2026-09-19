// static/js/voice/voiceAssistant.js
//
// The push-to-talk controller. One loop, in order:
//
//   mic  ->  capture  ->  transcript  ->  the EXISTING chat composer
//        ->  chat.js streams the reply (speaking it via aiTTSManager)
//        ->  back to idle
//
// Nothing here calls an LLM, opens a conversation store, or invents a tool
// path. A spoken request is transcribed and then submitted exactly as if it had
// been typed, so agent permissions, memory, RAG, email approval and every other
// guarantee carry over unchanged.

import {
  VOICE_STATES,
  VOICE_ERRORS,
  VOICE_DEFAULTS,
  createVoiceStateMachine,
  errorMessage,
} from './voiceTypes.js';
import { createCapture, preflight } from './audioCapture.js';
import { createPlayback } from './audioPlayback.js';
import { createVoiceClient } from './voiceClient.js';
import {
  submitTranscript,
  watchStreamLifecycle,
  readLastAssistantText,
  isStreaming,
} from './chatBridge.js';
import { isAvailable as browserSttAvailable, createRecognizer } from './browserStt.js';

/** Poll interval while waiting for speech to finish before returning to idle. */
const SPEECH_POLL_MS = 200;
/** Give up waiting for audio to start (a silent reply) after this long. */
const SPEECH_START_GRACE_MS = 4000;
/** Absolute ceiling on a spoken reply, so a stuck audio element cannot hang us. */
const SPEECH_MAX_MS = 10 * 60 * 1000;

export function createVoiceAssistant(options = {}) {
  const win = options.win || globalThis.window;
  const doc = options.doc || globalThis.document;
  const client = options.client || createVoiceClient();
  const playback = options.playback || createPlayback({ win });

  // `announce` is how the controller reports anything it could not do. The UI
  // turns these into toasts; tests use them as assertions.
  const announce = typeof options.announce === 'function' ? options.announce : () => {};

  const listeners = new Set();
  let settings = null;
  let voiceSessionId = null;
  let capture = null;
  let recognizer = null;
  let sttEngine = null;
  let speakReplies = true;
  let turnToken = 0;
  let liveTranscript = '';
  let lastError = null;
  let disconnectStreamWatch = null;
  let speechTimer = null;

  const machine = createVoiceStateMachine(VOICE_STATES.IDLE, () => emit());

  // ── observers ────────────────────────────────────────────────────────

  function snapshot() {
    return {
      state: machine.state,
      transcript: liveTranscript,
      error: lastError,
      sttEngine,
      ttsEngine: playback.resolveEngine(),
      speakReplies,
      settings,
      capturing: machine.isCapturing(),
      streaming: isStreaming(doc),
    };
  }

  function emit() {
    const snap = snapshot();
    listeners.forEach((fn) => {
      try {
        fn(snap);
      } catch (e) {
        // A broken listener must not break a live microphone session.
      }
    });
  }

  function subscribe(listener) {
    listeners.add(listener);
    try {
      listener(snapshot());
    } catch (e) {
      /* ignore */
    }
    return () => listeners.delete(listener);
  }

  function fail(code, { fatal = false } = {}) {
    lastError = { code, message: errorMessage(code) };
    machine.set(VOICE_STATES.ERROR, { force: true });
    if (code !== VOICE_ERRORS.ABORTED) {
      announce({ level: 'error', code, message: errorMessage(code), fatal });
    }
    return null;
  }

  function clearError() {
    if (lastError) {
      lastError = null;
      emit();
    }
  }

  // ── settings / engine choice ─────────────────────────────────────────

  /**
   * Load server capability once. Failure is non-fatal: we fall back to the
   * browser engine rather than making voice mode unusable because a settings
   * endpoint hiccuped.
   */
  async function refreshSettings() {
    try {
      settings = await client.getSettings();
    } catch (err) {
      settings = null;
      if (err && err.code === VOICE_ERRORS.NOT_AUTHENTICATED) {
        fail(VOICE_ERRORS.NOT_AUTHENTICATED);
        return null;
      }
    }
    emit();
    return settings;
  }

  /**
   * Pick the transcription engine.
   *
   * Server first — that is the local-first path. The browser engine is only
   * used when the server has nothing, and the caller surfaces that to the user.
   */
  function chooseSttEngine() {
    if (settings && settings.enabled === false) return null;
    if (settings && settings.stt_available) return 'server';
    if (browserSttAvailable(win)) return 'browser';
    return null;
  }

  /** The reason `chooseSttEngine` returned null, for the error message. */
  function sttFailureCode() {
    if (settings && settings.enabled === false) return VOICE_ERRORS.DISABLED;
    return VOICE_ERRORS.STT_UNAVAILABLE;
  }

  // ── session lifecycle ────────────────────────────────────────────────

  /** Bind the voice session to whatever conversation is open, if any. */
  function currentConversationId() {
    try {
      const sm = win && win.sessionModule;
      if (sm && typeof sm.getCurrentSessionId === 'function') {
        return sm.getCurrentSessionId() || null;
      }
    } catch (e) {
      /* no session module (e.g. tests) */
    }
    return null;
  }

  async function ensureSession() {
    if (voiceSessionId) return voiceSessionId;
    try {
      const created = await client.startSession({
        conversationId: currentConversationId(),
        language: settings ? settings.default_language : null,
        mode: 'push_to_talk',
        voice: settings ? settings.default_voice : null,
      });
      voiceSessionId = created && created.id ? created.id : null;
    } catch (err) {
      // Transcription needs a session, so this is fatal for this attempt.
      fail((err && err.code) || VOICE_ERRORS.PROVIDER_UNAVAILABLE, { fatal: true });
      return null;
    }
    return voiceSessionId;
  }

  async function endSession() {
    const id = voiceSessionId;
    voiceSessionId = null;
    if (!id) return;
    try {
      await client.endSession(id);
    } catch (e) {
      // Best effort: the server expires sessions anyway, and failing to clean
      // up must not surface as an error to someone who just stopped talking.
    }
  }

  // ── listening ────────────────────────────────────────────────────────

  /**
   * Open the mic. Safe to call when already listening (no-op), and it barges in
   * on a reply in progress — that is the interruption requirement.
   *
   * @returns {Promise<boolean>} whether the mic actually opened.
   */
  async function startListening({ interruptIfSpeaking = true } = {}) {
    if (machine.isCapturing()) return true;

    // Barge-in: cut the assistant off and stop its generation.
    if (interruptIfSpeaking && (playback.isSpeaking() || isStreaming(doc))) {
      interrupt({ reopen: false });
    }

    turnToken += 1;
    const token = turnToken;
    clearError();

    const blocked = preflight(win);
    if (blocked) {
      fail(blocked);
      return false;
    }

    if (!settings) await refreshSettings();

    sttEngine = chooseSttEngine();
    if (!sttEngine) {
      fail(sttFailureCode());
      return false;
    }

    // Server STT needs a session before it will accept audio.
    if (sttEngine === 'server') {
      const id = await ensureSession();
      if (!id || token !== turnToken) return false;
    }

    if (sttEngine === 'browser') {
      recognizer = createRecognizer({
        win,
        lang: (settings && settings.default_language) || '',
        onPartial: (text) => {
          liveTranscript = text;
          // Live text is a stronger signal than a raw level meter.
          if (text && machine.state === VOICE_STATES.LISTENING) {
            machine.set(VOICE_STATES.SPEECH_DETECTED);
          }
          emit();
        },
        onError: (code) => {
          if (token === turnToken) fail(code);
        },
      });
      if (!recognizer) {
        fail(VOICE_ERRORS.STT_UNAVAILABLE);
        return false;
      }
      recognizer.start();
    }

    capture = createCapture({
      win,
      maxDurationMs:
        (settings && settings.max_recording_seconds
          ? Number(settings.max_recording_seconds)
          : VOICE_DEFAULTS.maxRecordingSeconds) * 1000,
      onLevel: options.onLevel || null,
      onAutoStop: () => {
        // The duration cap fired; finish the turn instead of recording on.
        if (token === turnToken) finishListening();
      },
    });

    try {
      await capture.start();
    } catch (err) {
      capture = null;
      if (recognizer) {
        recognizer.abort();
        recognizer = null;
      }
      fail((err && err.code) || VOICE_ERRORS.INIT_FAILED);
      return false;
    }

    if (token !== turnToken) {
      // A cancel landed while getUserMedia was in flight.
      try {
        capture.cancel();
      } catch (e) {
        /* ignore */
      }
      capture = null;
      return false;
    }

    liveTranscript = '';
    machine.set(VOICE_STATES.LISTENING, { force: true });
    return true;
  }

  /**
   * Close the mic, transcribe, and hand the text to the chat.
   *
   * @returns {Promise<string|null>} the submitted transcript.
   */
  async function finishListening() {
    if (!capture && !recognizer) {
      machine.set(VOICE_STATES.IDLE, { force: true });
      return null;
    }

    const token = ++turnToken;
    const engine = sttEngine;
    const activeCapture = capture;
    capture = null;

    let blob = null;
    let autoStopped = false;
    if (activeCapture) {
      try {
        const result = await activeCapture.stop();
        blob = result.blob;
        autoStopped = result.autoStopped;
      } catch (e) {
        autoStopped = false;
      }
    }

    if (autoStopped) {
      announce({
        level: 'info',
        code: VOICE_ERRORS.RECORDING_TOO_LONG,
        message: errorMessage(VOICE_ERRORS.RECORDING_TOO_LONG),
      });
    }

    machine.set(VOICE_STATES.PROCESSING, { force: true });

    let text = '';
    if (engine === 'browser') {
      const active = recognizer;
      recognizer = null;
      if (active) {
        try {
          text = await active.stop();
        } catch (e) {
          text = active.text || '';
        }
      }
    } else {
      if (!blob || !blob.size) {
        return fail(VOICE_ERRORS.EMPTY_TRANSCRIPT);
      }
      const id = await ensureSession();
      if (!id) return null;
      try {
        const result = await client.transcribe(id, blob, 'utterance.webm');
        text = (result && result.text) || '';
      } catch (err) {
        return fail((err && err.code) || VOICE_ERRORS.PROVIDER_UNAVAILABLE);
      }
    }

    if (token !== turnToken) return null; // cancelled mid-transcription

    text = String(text || '').trim();
    if (!text) return fail(VOICE_ERRORS.EMPTY_TRANSCRIPT);

    liveTranscript = text;
    emit();
    return submitSpokenText(text);
  }

  /** Abandon the current utterance without sending anything (Escape). */
  function cancel() {
    turnToken += 1;
    if (recognizer) {
      recognizer.abort();
      recognizer = null;
    }
    if (capture) {
      try {
        capture.cancel();
      } catch (e) {
        /* ignore */
      }
      capture = null;
    }
    liveTranscript = '';
    emit();
    machine.set(VOICE_STATES.IDLE, { force: true });
    if (voiceSessionId) endSession();
  }

  // ── speaking / interruption ──────────────────────────────────────────

  /** Stop playback immediately (used on its own by the mute/stop button). */
  function stopSpeaking() {
    playback.stop();
    clearSpeechTimer();
    emit();
  }

  /**
   * Barge-in: silence the reply, cancel generation, and hand the floor back.
   *
   * @param {object} [opts]
   * @param {boolean} [opts.reopen] start listening again after stopping.
   */
  function interrupt({ reopen = false } = {}) {
    playback.stop();
    clearSpeechTimer();

    // Same call the Stop button makes, so the server-side detached run is
    // cancelled too rather than continuing to burn tokens in the background.
    try {
      const chat = win && win.chatModule;
      if (chat && typeof chat.abortCurrentRequest === 'function' && isStreaming(doc)) {
        chat.abortCurrentRequest(true);
      }
    } catch (e) {
      /* stopping is best effort */
    }

    machine.set(VOICE_STATES.INTERRUPTED, { force: true });
    if (reopen) {
      startListening({ interruptIfSpeaking: false });
    } else {
      machine.set(VOICE_STATES.IDLE, { force: true });
    }
  }

  function clearSpeechTimer() {
    if (speechTimer) {
      win.clearTimeout(speechTimer);
      speechTimer = null;
    }
  }

  /**
   * Wait until the spoken reply finishes, then return to idle.
   *
   * Audio can start a beat after the text stream ends, so we wait for it to
   * begin (briefly) before waiting for it to stop.
   */
  function awaitSpokenTurn(token) {
    return new Promise((resolve) => {
      const started = Date.now();
      let sawSpeech = false;

      const poll = () => {
        if (token !== turnToken) {
          resolve();
          return;
        }
        const speaking = playback.isSpeaking();
        if (speaking) sawSpeech = true;
        const elapsed = Date.now() - started;

        if (sawSpeech && !speaking) {
          resolve();
          return;
        }
        if (!sawSpeech && elapsed > SPEECH_START_GRACE_MS) {
          resolve();
          return;
        }
        if (elapsed > SPEECH_MAX_MS) {
          resolve();
          return;
        }
        speechTimer = win.setTimeout(poll, SPEECH_POLL_MS);
      };
      poll();
    });
  }

  // ── turn wiring ──────────────────────────────────────────────────────

  /**
   * Watch the chat stream so voice state tracks the reply.
   *
   * Two engines, deliberately:
   *   * `server` — aiTTSManager is live, so turning on `autoPlay` makes chat.js
   *     speak the reply sentence by sentence while it streams. Best latency,
   *     zero duplication, and `stop()` is a true barge-in.
   *   * `browser` — the app's TTS stack is unavailable, so we read the finished
   *     text out of the DOM and let the OS voice say it.
   */
  function prepareSpokenTurn() {
    const token = ++turnToken;
    const engine = playback.resolveEngine();

    // ORDER MATTERS. chat.js reads `aiTTSManager.autoPlay` synchronously when
    // the stream begins, so autoplay has to be on BEFORE the message is
    // submitted. Enabling it afterwards silently produces no speech at all.
    if (speakReplies && engine === 'server') {
      playback.enableAutoplay();
    } else if (speakReplies && engine !== 'browser') {
      announce({
        level: 'error',
        code: VOICE_ERRORS.TTS_UNAVAILABLE,
        message: errorMessage(VOICE_ERRORS.TTS_UNAVAILABLE),
      });
    }

    disconnectStreamWatch = watchStreamLifecycle({
      doc,
      onStart: () => {
        if (token !== turnToken) return;
        if (speakReplies) machine.set(VOICE_STATES.SPEAKING, { force: true });
        emit();
      },
      onEnd: async () => {
        if (token !== turnToken) return;
        if (speakReplies && engine === 'browser') {
          const text = readLastAssistantText(doc);
          if (text) {
            machine.set(VOICE_STATES.SPEAKING, { force: true });
            try {
              await playback.speakFallback(text);
            } catch (e) {
              /* reported below */
            }
          }
        }
        await awaitSpokenTurn(token);
        if (token !== turnToken) return;
        playback.restoreAutoplay();
        machine.set(VOICE_STATES.IDLE, { force: true });
      },
    });
    return token;
  }

  function teardown() {
    if (disconnectStreamWatch) {
      disconnectStreamWatch();
      disconnectStreamWatch = null;
    }
  }

  /**
   * Submit `text` as a spoken turn: arm speech first, then send.
   *
   * This is the single entry point both the push-to-talk loop and the
   * dev-console helper go through, so they cannot drift apart.
   */
  function submitSpokenText(text) {
    const clean = String(text || '').trim();
    if (!clean) return null;

    liveTranscript = clean;
    prepareSpokenTurn();

    const submitted = submitTranscript(clean, { append: true, doc });
    if (!submitted) {
      // Nothing was sent, so unwind the speech we just armed.
      teardown();
      playback.restoreAutoplay();
      machine.set(VOICE_STATES.IDLE, { force: true });
      return null;
    }
    return submitted;
  }

  return {
    // lifecycle
    init: refreshSettings,
    teardown,
    subscribe,
    getSnapshot: snapshot,
    refreshSettings,

    // capture loop
    startListening,
    finishListening,
    cancel,
    get isListening() {
      return machine.isCapturing();
    },

    // speech
    submitText: submitSpokenText,
    interrupt,
    stopSpeaking,
    setSpeakReplies(value) {
      speakReplies = value !== false;
      if (!speakReplies) stopSpeaking();
      emit();
    },
    get speakReplies() {
      return speakReplies;
    },

    // session
    currentSessionId: () => voiceSessionId,
    endSession,

    // introspection for the UI
    get state() {
      return machine.state;
    },
    get lastError() {
      return lastError;
    },
    getSttEngine: () => sttEngine,
  };
}

export default createVoiceAssistant;
