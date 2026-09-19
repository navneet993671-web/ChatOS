// static/js/voice/audioPlayback.js
//
// Speech output for voice mode.
//
// Design note, because this looks thinner than expected on purpose:
//
// The existing chat stream ALREADY speaks assistant replies sentence by
// sentence. `static/js/chat.js` checks `window.aiTTSManager.autoPlay` before a
// stream starts, calls `streamingStart()`, feeds each round through
// `streamingUpdate()` and flushes with `streamingEnd()` at the end. So voice
// mode does not need to synthesise anything itself — it needs to turn that
// existing behaviour on while a spoken turn is in flight, and be able to stop
// it instantly for barge-in.
//
// Synthesising here as well would double-speak every reply and duplicate the
// TTS stack the product already has. The only case this module speaks on its
// own is the fallback: no server/browser TTS configured, but the OS still has
// a built-in voice. That keeps voice mode audible on a stock install without
// silently turning on a provider the user disabled.

/** Which engine will actually produce sound, if any. */
export function resolveEngine(win = globalThis.window) {
  if (!win) return null;
  const mgr = win.aiTTSManager;
  if (mgr && mgr.available && mgr._provider && mgr._provider !== 'disabled') {
    return 'server';
  }
  if (win.speechSynthesis && typeof win.SpeechSynthesisUtterance === 'function') {
    return 'browser';
  }
  return null;
}

/**
 * @param {object} [options]
 * @param {object} [options.win] injectable window (tests)
 * @param {number} [options.rate] speaking speed for the fallback voice
 * @param {string} [options.lang] BCP-47 tag for the fallback voice
 */
export function createPlayback(options = {}) {
  const win = options.win || globalThis.window;
  // Whether *we* turned autoplay on, so ending voice mode does not clobber a
  // user who had TTS Mode enabled for text chat already.
  let autoplayOwned = false;
  let fallbackCurrent = null;

  function manager() {
    return win && win.aiTTSManager;
  }

  /** Turn on the existing streaming TTS for the duration of a spoken turn. */
  function enableAutoplay() {
    const mgr = manager();
    if (!mgr) return false;
    if (mgr.autoPlay !== true) {
      mgr.autoPlay = true;
      autoplayOwned = true;
    }
    return true;
  }

  /** Undo enableAutoplay(), but only if we were the ones who set it. */
  function restoreAutoplay() {
    const mgr = manager();
    if (mgr && autoplayOwned) mgr.autoPlay = false;
    autoplayOwned = false;
  }

  /** Is audio coming out right now? Drives the "Speaking…" state. */
  function isSpeaking() {
    const mgr = manager();
    if (mgr && (mgr.isPlaying || mgr._processing)) return true;
    if (fallbackCurrent) return true;
    if (win && win.speechSynthesis && win.speechSynthesis.speaking) return true;
    return false;
  }

  /**
   * Stop all speech immediately.
   *
   * This is the barge-in primitive: the caller opens the mic and calls this in
   * the same tick, so the assistant is cut off rather than talked over.
   */
  function stop() {
    const mgr = manager();
    if (mgr && typeof mgr.stop === 'function') {
      try {
        mgr.stop();
      } catch (e) {
        /* already stopped */
      }
    }
    if (win && win.speechSynthesis && typeof win.speechSynthesis.cancel === 'function') {
      try {
        win.speechSynthesis.cancel();
      } catch (e) {
        /* ignore */
      }
    }
    fallbackCurrent = null;
  }

  /**
   * Speak `text` with the OS voice. Only used when the app's TTS stack is
   * unavailable — the normal path is streaming TTS driven by chat.js.
   */
  function speakFallback(text) {
    return new Promise((resolve, reject) => {
      if (!win || !win.speechSynthesis) {
        const err = new Error('tts_unavailable');
        err.code = 'tts_unavailable';
        reject(err);
        return;
      }
      const clean = String(text || '').trim();
      if (!clean) {
        resolve();
        return;
      }
      try {
        const utterance = new win.SpeechSynthesisUtterance(clean);
        utterance.rate = options.rate || 1;
        if (options.lang) utterance.lang = options.lang;
        utterance.onend = () => {
          fallbackCurrent = null;
          resolve();
        };
        utterance.onerror = (event) => {
          fallbackCurrent = null;
          // A user-initiated cancel is not an error worth surfacing.
          if (event && (event.error === 'interrupted' || event.error === 'canceled')) {
            resolve();
            return;
          }
          const err = new Error('tts_unavailable');
          err.code = 'tts_unavailable';
          reject(err);
        };
        fallbackCurrent = utterance;
        win.speechSynthesis.speak(utterance);
      } catch (e) {
        fallbackCurrent = null;
        reject(e);
      }
    });
  }

  return {
    resolveEngine: () => resolveEngine(win),
    enableAutoplay,
    restoreAutoplay,
    isSpeaking,
    stop,
    speakFallback,
    get ownsAutoplay() {
      return autoplayOwned;
    },
  };
}
