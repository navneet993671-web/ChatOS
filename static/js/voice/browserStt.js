// static/js/voice/browserStt.js
//
// Optional fallback transcription using the browser's built-in speech
// recognition (`SpeechRecognition` / `webkitSpeechRecognition`).
//
// WHY THIS IS OFF UNLESS IT HAS TO BE: the Web Speech API is not guaranteed to
// run on-device. In Chrome it streams audio to Google. This product is
// local-first, so the server provider (`VOICE_STT_PROVIDER=local`, faster-whisper
// on your own machine) is always preferred, and voiceAssistant.js only falls
// back here when the server has no provider at all — announcing that fact in
// the overlay rather than quietly uploading the microphone.
//
// Note the shape difference from the server engine: recognition is *live*, so
// it has to be started alongside recording, not handed a finished blob.

/** Is the browser able to recognise speech at all? */
export function isAvailable(win = globalThis.window) {
  if (!win) return false;
  return !!(win.SpeechRecognition || win.webkitSpeechRecognition);
}

/**
 * Create a recognizer bound to one utterance.
 *
 * @param {object} [options]
 * @param {object} [options.win]
 * @param {string} [options.lang] BCP-47 tag; '' lets the browser decide.
 * @param {function} [options.onPartial] live text, called on every result
 * @param {function} [options.onError] receives a VOICE_ERRORS code
 */
export function createRecognizer(options = {}) {
  const win = options.win || globalThis.window;
  const Ctor = win && (win.SpeechRecognition || win.webkitSpeechRecognition);
  if (!Ctor) return null;

  let recognition = null;
  let finalText = '';
  let stopped = false;
  let sawResult = false;
  let resolveStop = null;

  function handleResult(event) {
    let interim = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      const piece = (result[0] && result[0].transcript) || '';
      if (result.isFinal) {
        finalText += piece;
        sawResult = true;
      } else {
        interim += piece;
      }
    }
    if (typeof options.onPartial === 'function') {
      const live = (finalText + interim).trim();
      if (live) options.onPartial(live);
    }
  }

  function handleError(event) {
    const name = (event && event.error) || '';
    // `no-speech` and `aborted` are normal ends of an utterance.
    if (name === 'no-speech' || name === 'aborted') return;
    if (typeof options.onError === 'function') {
      if (name === 'not-allowed' || name === 'service-not-allowed') {
        options.onError('permission_denied');
      } else if (name === 'audio-capture') {
        options.onError('no_microphone');
      } else if (name === 'network') {
        options.onError('backend_unavailable');
      } else {
        options.onError('unknown');
      }
    }
  }

  function start() {
    try {
      recognition = new Ctor();
    } catch (e) {
      return false;
    }
    recognition.continuous = true;
    recognition.interimResults = true;
    if (options.lang) recognition.lang = options.lang;
    recognition.onresult = handleResult;
    recognition.onerror = handleError;
    recognition.onend = () => {
      // Deliver whatever we got, even if the engine ended on its own.
      if (resolveStop) {
        const done = resolveStop;
        resolveStop = null;
        done(finalText.trim());
      }
    };
    try {
      recognition.start();
    } catch (e) {
      // Chrome throws if a recognizer is already running.
      return false;
    }
    return true;
  }

  /** Stop and resolve with the accumulated transcript. */
  function stop() {
    return new Promise((resolve) => {
      if (!recognition || stopped) {
        resolve(finalText.trim());
        return;
      }
      stopped = true;
      resolveStop = resolve;
      // Give the engine a moment to flush a final result before onend.
      const safety = win.setTimeout(() => {
        if (resolveStop === resolve) {
          resolveStop = null;
          resolve(finalText.trim());
        }
      }, 1200);
      try {
        recognition.stop();
      } catch (e) {
        win.clearTimeout(safety);
        resolveStop = null;
        resolve(finalText.trim());
      }
    });
  }

  /** Drop the utterance without producing a transcript. */
  function abort() {
    stopped = true;
    resolveStop = null;
    if (recognition) {
      try {
        recognition.abort();
      } catch (e) {
        /* ignore */
      }
      recognition = null;
    }
    finalText = '';
  }

  return {
    start,
    stop,
    abort,
    get text() {
      return finalText.trim();
    },
    /** True once at least one final result arrived. */
    get heardSomething() {
      return sawResult;
    },
  };
}
