// static/js/voice/chatBridge.js
//
// The bridge between voice and the chat that already exists.
//
// This is the load-bearing decision in the whole feature: a spoken request is
// not sent to some voice-specific endpoint. It is typed into the same
// `#message` composer and submitted through the same path a keystroke takes,
// so it reaches the same context engine, agent runtime, tools, memory, RAG and
// permission checks as typed text. Nothing about the AI stack is duplicated.
//
// We deliberately do not import chat.js. Calling `chatModule.handleChatSubmit`
// directly would skip the wrapper `app.js` installs on the form (group chat,
// compare mode, and the double-submit guard), so we drive the DOM the same way
// a user does and let every existing hook run.

/** Resolve the composer controls, or nulls when the page has no chat UI. */
export function getComposer(doc = globalThis.document) {
  if (!doc) return { input: null, sendBtn: null, form: null };
  return {
    input: doc.getElementById('message'),
    sendBtn: doc.querySelector('.send-btn'),
    form: doc.getElementById('chat-form'),
  };
}

/**
 * Put `text` in the composer, optionally keeping whatever the user already
 * typed, and submit it.
 *
 * Appending rather than replacing matters: dictating a follow-up must not
 * silently discard a half-written message. It is also how "continue as text"
 * works — the transcript lands in the box and you can keep typing.
 *
 * @returns {string|null} the text that was submitted, or null if there was
 *   nothing to send.
 */
export function submitTranscript(text, { append = true, doc = globalThis.document } = {}) {
  const clean = String(text || '').trim();
  if (!clean) return null;

  const { input, sendBtn, form } = getComposer(doc);
  if (!input) return null;

  const existing = append ? String(input.value || '').trim() : '';
  input.value = existing ? existing + ' ' + clean : clean;

  // Let the composer's own listeners run: auto-resize, send-icon swap, and the
  // model-picker autohide all key off this event.
  input.dispatchEvent(new Event('input', { bubbles: true }));

  // requestSubmit() is "press the form's submit button" without a synthetic
  // click; the fallback covers older browsers.
  if (form && typeof form.requestSubmit === 'function') {
    try {
      form.requestSubmit();
      return input.value.trim() || clean;
    } catch (e) {
      /* fall through to the click path */
    }
  }
  if (sendBtn) {
    sendBtn.click();
    return input.value.trim() || clean;
  }
  return null;
}

/** True while the assistant's reply is streaming. */
export function isStreaming(doc = globalThis.document) {
  const { sendBtn } = getComposer(doc);
  if (!sendBtn) return false;
  const mode = sendBtn.dataset ? sendBtn.dataset.mode : null;
  return mode === 'streaming' || mode === 'recording';
}

/**
 * Watch the send button's `data-mode`, which `chat.js` sets to `streaming`
 * when a reply starts and clears when it finishes.
 *
 * Observing an existing, documented signal beats adding a callback into
 * chat.js: the 4.5k-line streaming path stays untouched, which is the whole
 * point of wiring voice through the DOM.
 *
 * @returns {function} disconnect
 */
export function watchStreamLifecycle({ onStart, onEnd, doc = globalThis.document } = {}) {
  const { sendBtn } = getComposer(doc);
  if (!sendBtn || typeof MutationObserver !== 'function') {
    return () => {};
  }

  let last = sendBtn.dataset ? sendBtn.dataset.mode : '';
  const observer = new MutationObserver(() => {
    const mode = sendBtn.dataset ? sendBtn.dataset.mode : '';
    if (mode === last) return;
    const wasStreaming = last === 'streaming';
    const isNowStreaming = mode === 'streaming';
    last = mode;

    if (!wasStreaming && isNowStreaming) {
      if (typeof onStart === 'function') onStart();
    } else if (wasStreaming && !isNowStreaming) {
      if (typeof onEnd === 'function') onEnd();
    }
  });

  observer.observe(sendBtn, { attributes: true, attributeFilter: ['data-mode'] });
  return () => observer.disconnect();
}

/**
 * The text of the most recent assistant message.
 *
 * `chat.js` writes the raw (pre-markdown) text to `dataset.raw` on every
 * assistant bubble, which is exactly what the transcript panel wants.
 */
export function readLastAssistantText(doc = globalThis.document) {
  if (!doc) return '';
  const history = doc.getElementById('chat-history');
  if (!history) return '';
  const nodes = history.querySelectorAll('.msg-ai');
  for (let i = nodes.length - 1; i >= 0; i--) {
    const raw = nodes[i].dataset ? nodes[i].dataset.raw : '';
    if (raw && raw.trim()) return raw.trim();
  }
  return '';
}

/** Is anything still being generated, per the send button? */
export function isGenerating(doc = globalThis.document) {
  return isStreaming(doc);
}
