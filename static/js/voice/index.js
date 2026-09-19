// static/js/voice/index.js
//
// Entry point for Misantropic voice mode. Loaded as a module from index.html
// and initialised from app.js next to the other chat modules.
//
// Scope of this slice: push-to-talk over the existing REST voice endpoints and
// the existing chat pipeline. Continuous "voice conversation" mode with
// automatic endpointing needs the WebSocket transport and is a later slice —
// see README (Voice Assistant) for what is and is not wired up.

import createVoiceAssistant from './voiceAssistant.js';
import createVoiceUI from './voiceUI.js';
import uiModule from '../ui.js';

let assistant = null;
let voiceUI = null;
let bound = false;
let lastAnnounced = { code: null, at: 0 };

/** Collapse duplicate announcements so a retry loop cannot spam the toasts. */
function announceToUser(event) {
  if (!event) return;
  const now = Date.now();
  if (event.code && event.code === lastAnnounced.code && now - lastAnnounced.at < 4000) {
    return;
  }
  lastAnnounced = { code: event.code || null, at: now };

  if (event.level === 'error') {
    if (uiModule && uiModule.showError) uiModule.showError(event.message);
  } else if (uiModule && uiModule.showToast) {
    uiModule.showToast(event.message);
  }
}

/**
 * Initialise voice mode.
 *
 * Idempotent and defensive: the tests, the login page and any embed without a
 * chat composer all load static modules, and none of them should throw because
 * a microphone button is missing.
 */
export function init() {
  if (bound) return assistant;
  if (typeof document === 'undefined') return null;

  const button = document.getElementById('voice-btn');
  const overlay = document.getElementById('voice-overlay');
  if (!button && !overlay) return null;

  assistant = createVoiceAssistant({
    announce: announceToUser,
    // Late-bound: the UI is constructed just below, and the mic level is only
    // meaningful once it exists.
    onLevel: (value) => {
      if (voiceUI) voiceUI.setLevel(value);
    },
  });

  voiceUI = createVoiceUI({ assistant, ui: uiModule });
  voiceUI.bind();

  // Hide the control entirely when the operator disabled voice, so nobody
  // clicks a button that can only ever fail.
  assistant.subscribe((snap) => {
    if (!snap.settings) return;
    if (snap.settings.enabled === false) {
      button.hidden = true;
      button.title = 'Voice is disabled on this server';
    }
  });

  assistant.init();

  // Release the microphone and stop any speech if the page goes away.
  const cleanup = () => {
    try {
      assistant.cancel();
    } catch (e) {
      /* ignore */
    }
    assistant.teardown();
  };
  window.addEventListener('pagehide', cleanup);
  window.addEventListener('beforeunload', cleanup);

  // Exposed for debugging and the frontend tests, matching how chat.js and
  // app.js publish `window.chatModule` / `window.sessionModule`.
  window.voiceAssistant = assistant;
  window.voiceUI = voiceUI;

  bound = true;
  return assistant;
}

export { assistant, voiceUI };

const voiceModule = { init, get assistant() { return assistant; }, get voiceUI() { return voiceUI; } };
export default voiceModule;
