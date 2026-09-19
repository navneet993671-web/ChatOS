// static/js/voice/voiceUI.js
//
// The visible half: a mic button in the chat composer and an overlay that
// reports what voice mode is doing.
//
// Accessibility rules this follows, because a microphone is a stateful control
// and a colour change is not a status:
//   * the button is a real <button> with aria-pressed, so keyboard and screen
//     readers can both drive it;
//   * every state has a text label in the overlay, announced via aria-live;
//   * the waveform is decorative (aria-hidden) and never the only signal;
//   * errors are sentences, not colours, and name the recovery action;
//   * the transcript is always visible, copyable and editable, so a
//     mis-transcribed word never traps the user in audio.

import { VOICE_STATES, VOICE_STATE_LABELS, VOICE_DEFAULTS } from './voiceTypes.js';
import { readLastAssistantText } from './chatBridge.js';

/** Bars in the waveform. Purely decorative. */
const WAVE_BARS = 21;

/** Per-bar weight so the meter reads as a wave rather than a solid block. */
function barWeights(count) {
  const out = [];
  for (let i = 0; i < count; i++) {
    const t = i / (count - 1);
    // Peaks in the middle, tapering at the edges.
    out.push(0.35 + 0.65 * Math.sin(Math.PI * t));
  }
  return out;
}

export function createVoiceUI({ assistant, doc = globalThis.document, win = globalThis.window, ui = null }) {
  const el = (id) => (doc ? doc.getElementById(id) : null);

  const btn = el('voice-btn');
  const overlay = el('voice-overlay');
  const statusEl = el('voice-status');
  const dotEl = el('voice-status-dot');
  const waveEl = el('voice-wave');
  const noticeEl = el('voice-notice');
  const userTextEl = el('voice-transcript-user');
  const assistantTextEl = el('voice-transcript-assistant');
  const primaryBtn = el('voice-primary');
  const copyBtn = el('voice-copy');
  const continueBtn = el('voice-continue');
  const muteBtn = el('voice-mute');
  const closeBtn = el('voice-close');

  const weights = barWeights(WAVE_BARS);
  let bars = [];
  let level = 0;
  let rafId = null;
  let latched = false;
  let holding = false;
  let pressStart = 0;
  let unsubscribe = null;

  // ── waveform ─────────────────────────────────────────────────────────

  function buildWave() {
    if (!waveEl) return;
    waveEl.innerHTML = '';
    bars = [];
    for (let i = 0; i < WAVE_BARS; i++) {
      const bar = doc.createElement('span');
      bar.className = 'voice-wave-bar';
      bar.style.height = '8%';
      waveEl.appendChild(bar);
      bars.push(bar);
    }
  }

  function paintWave() {
    if (!bars.length) return;
    // Idle/speaking states get a gentle pulse so the overlay never looks frozen.
    const state = assistant.state;
    const decorative =
      state === VOICE_STATES.PROCESSING || state === VOICE_STATES.SPEAKING;
    const t = Date.now() / 220;
    for (let i = 0; i < bars.length; i++) {
      let value = level * weights[i];
      if (decorative) {
        value = 0.18 + 0.34 * Math.abs(Math.sin(t + i * 0.55)) * weights[i] * 3;
      }
      const pct = Math.max(6, Math.min(100, value * 100));
      bars[i].style.height = pct.toFixed(1) + '%';
    }
    rafId = win.requestAnimationFrame
      ? win.requestAnimationFrame(paintWave)
      : win.setTimeout(paintWave, 60);
  }

  function startWave() {
    if (rafId != null) return;
    paintWave();
  }

  function stopWave() {
    if (rafId == null) return;
    if (win.cancelAnimationFrame) win.cancelAnimationFrame(rafId);
    else win.clearTimeout(rafId);
    rafId = null;
    level = 0;
    for (let i = 0; i < bars.length; i++) bars[i].style.height = '8%';
  }

  // ── rendering ────────────────────────────────────────────────────────

  function setNotice(kind, text) {
    if (!noticeEl) return;
    if (!text) {
      noticeEl.classList.add('hidden');
      noticeEl.textContent = '';
      return;
    }
    noticeEl.classList.remove('hidden');
    noticeEl.setAttribute('data-kind', kind || 'info');
    noticeEl.textContent = text;
  }

  /**
   * Notices and the primary action are derived from state, so the overlay
   * cannot disagree with the controller.
   */
  function render(snap) {
    const state = snap.state;
    const label = VOICE_STATE_LABELS[state] || state;

    if (statusEl) statusEl.textContent = label;
    if (dotEl) dotEl.setAttribute('data-state', state);
    if (overlay) overlay.setAttribute('data-state', state);

    if (btn) {
      const pressed = snap.capturing ? 'true' : 'false';
      btn.setAttribute('aria-pressed', pressed);
      btn.classList.toggle('recording', !!snap.capturing);
      btn.classList.toggle('speaking', state === VOICE_STATES.SPEAKING);
      btn.title = snap.capturing
        ? 'Stop listening (or press Escape to cancel)'
        : 'Voice input \u2014 hold to talk, or click to latch';
    }

    // Overlay visibility: open while a spoken turn is in progress, and stay up
    // briefly in the error state so the reason is readable.
    const visible =
      snap.capturing ||
      state === VOICE_STATES.PROCESSING ||
      state === VOICE_STATES.SPEAKING ||
      state === VOICE_STATES.ERROR;
    if (overlay) {
      overlay.classList.toggle('hidden', !visible);
      overlay.setAttribute('aria-hidden', visible ? 'false' : 'true');
    }

    if (snap.capturing) startWave();
    else stopWave();

    if (primaryBtn) {
      if (snap.capturing) {
        primaryBtn.textContent = 'Stop & send';
        primaryBtn.disabled = false;
      } else if (state === VOICE_STATES.SPEAKING || snap.streaming) {
        primaryBtn.textContent = 'Interrupt';
        primaryBtn.disabled = false;
      } else if (state === VOICE_STATES.PROCESSING) {
        primaryBtn.textContent = 'Working\u2026';
        primaryBtn.disabled = true;
      } else {
        primaryBtn.textContent = 'Start listening';
        primaryBtn.disabled = false;
      }
    }

    if (userTextEl && snap.transcript) userTextEl.textContent = snap.transcript;
    if (continueBtn) continueBtn.disabled = !snap.transcript;

    // The reply only exists once it has streamed, so re-read it in the states
    // that follow the request rather than at submit time.
    if (
      state === VOICE_STATES.SPEAKING ||
      state === VOICE_STATES.PROCESSING ||
      state === VOICE_STATES.IDLE
    ) {
      refreshAssistantText();
    }

    // Errors replace any capability notice: one thing to read at a time.
    if (snap.error) {
      setNotice('error', snap.error.message);
    } else if (snap.sttEngine === 'browser') {
      setNotice(
        'warning',
        'Using the browser speech engine. Audio may be processed off-device \u2014 ' +
          'install a local speech-to-text model for fully offline transcription.'
      );
    } else if (snap.ttsEngine === null && snap.speakReplies) {
      setNotice(
        'warning',
        'No text-to-speech is configured, so replies will not be spoken. ' +
          'Pick a voice engine in Settings \u2014 browser or local \u2014 to hear them.'
      );
    } else {
      setNotice('info', '');
    }

    if (muteBtn) {
      muteBtn.textContent = snap.speakReplies ? 'Mute replies' : 'Unmute replies';
      muteBtn.setAttribute('aria-pressed', snap.speakReplies ? 'false' : 'true');
    }
  }

  /**
   * Fill the assistant half of the transcript from the chat history.
   *
   * chat.js writes the raw reply to `dataset.raw` on every assistant bubble,
   * so reading the last one keeps the overlay in sync with what is on screen
   * without a second copy of the text living in voice state.
   */
  function refreshAssistantText() {
    if (!assistantTextEl) return;
    const text = readLastAssistantText(doc);
    if (text) assistantTextEl.textContent = text;
  }

  // ── push-to-talk input ───────────────────────────────────────────────

  const HOLD = VOICE_DEFAULTS.tapHoldThresholdMs;

  async function handlePointerDown(event) {
    if (event.button != null && event.button !== 0) return;
    if (assistant.isListening) {
      // Second press of a latched recording: send it.
      latched = false;
      await assistant.finishListening();
      return;
    }
    pressStart = Date.now();
    holding = true;
    if (btn && event.pointerId != null && btn.setPointerCapture) {
      try {
        btn.setPointerCapture(event.pointerId);
      } catch (e) {
        /* not fatal */
      }
    }
    const opened = await assistant.startListening();
    if (!opened) holding = false;
  }

  async function handlePointerUp() {
    if (!holding) return;
    holding = false;
    const held = Date.now() - pressStart;
    if (held >= HOLD) {
      // Held down: classic push-to-talk, release sends.
      latched = false;
      await assistant.finishListening();
    } else {
      // Quick tap: latch recording on so the user can speak at length.
      latched = true;
    }
  }

  function bind() {
    buildWave();

    if (btn) {
      btn.addEventListener('pointerdown', handlePointerDown);
      btn.addEventListener('pointerup', handlePointerUp);
      btn.addEventListener('pointercancel', handlePointerUp);
      // Keyboard and assistive tech activate a button with click + detail 0;
      // pointer activation also fires click, so only detail 0 is handled here.
      btn.addEventListener('click', async (event) => {
        if (event.detail > 0) return;
        event.preventDefault();
        if (assistant.isListening) {
          latched = false;
          await assistant.finishListening();
        } else {
          const opened = await assistant.startListening();
          latched = opened;
        }
      });
    }

    if (primaryBtn) {
      primaryBtn.addEventListener('click', async () => {
        if (assistant.isListening) {
          latched = false;
          await assistant.finishListening();
          return;
        }
        if (assistant.state === VOICE_STATES.SPEAKING) {
          assistant.interrupt();
          return;
        }
        await assistant.startListening();
      });
    }

    if (muteBtn) {
      muteBtn.addEventListener('click', () => {
        assistant.setSpeakReplies(!assistant.speakReplies);
      });
    }

    if (closeBtn) {
      closeBtn.addEventListener('click', () => {
        latched = false;
        assistant.cancel();
      });
    }

    if (copyBtn) {
      copyBtn.addEventListener('click', async () => {
        const snap = assistant.getSnapshot();
        const reply = assistantTextEl ? assistantTextEl.textContent || '' : '';
        const lines = [];
        if (snap.transcript) lines.push('You: ' + snap.transcript);
        if (reply) lines.push('Misantropic: ' + reply);
        const text = lines.join('\n\n');
        if (!text) return;
        try {
          await (win.navigator && win.navigator.clipboard
            ? win.navigator.clipboard.writeText(text)
            : Promise.reject(new Error('no clipboard')));
          if (ui && ui.showToast) ui.showToast('Transcript copied');
        } catch (e) {
          if (ui && ui.showError) ui.showError('Could not copy the transcript');
        }
      });
    }

    if (continueBtn) {
      // "Continue as text": drop the transcript into the composer without
      // sending, so it can be corrected before it goes anywhere.
      continueBtn.addEventListener('click', () => {
        const snap = assistant.getSnapshot();
        if (!snap.transcript) return;
        const input = doc.getElementById('message');
        if (input) {
          const existing = String(input.value || '').trim();
          input.value = existing ? existing + ' ' + snap.transcript : snap.transcript;
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.focus();
        }
        latched = false;
        assistant.cancel();
        if (ui && ui.showToast) ui.showToast('Transcript moved to the message box');
      });
    }

    // Escape cancels a recording, or stops a spoken reply.
    doc.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      if (assistant.isListening) {
        latched = false;
        assistant.cancel();
      } else if (assistant.state === VOICE_STATES.SPEAKING) {
        assistant.interrupt();
      }
    });

    unsubscribe = assistant.subscribe(render);
    return () => {
      if (unsubscribe) unsubscribe();
      stopWave();
    };
  }

  return {
    bind,
    render,
    setLevel(value) {
      level = Math.max(0, Math.min(1, Number(value) || 0));
    },
    /** Refresh the assistant side of the transcript from the chat history. */
    setAssistantText(text) {
      if (assistantTextEl) assistantTextEl.textContent = text || '';
    },
    get latched() {
      return latched;
    },
  };
}

export default createVoiceUI;
