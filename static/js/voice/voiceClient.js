// static/js/voice/voiceClient.js
//
// Thin REST client for the `/api/voice/*` routes (routes/voice.py).
//
// It never sends a user id: the server derives the owner from the authenticated
// session and 404s anything that is not yours, so there is nothing for the
// client to assert about identity.
//
// Every failure becomes a `VoiceApiError` carrying a machine-readable `code`
// from VOICE_ERRORS. The UI renders `errorMessage(code)` — a provider blowing up
// must not be able to take the Chat page down with it.

import { VOICE_ERRORS } from './voiceTypes.js';

const API_PREFIX = '/api/voice';

/** Failure from the voice API, already classified for the UI. */
export class VoiceApiError extends Error {
  constructor(code, message, { status = 0, cause = null } = {}) {
    super(message || code);
    this.name = 'VoiceApiError';
    this.code = code;
    this.status = status;
    this.cause = cause;
  }
}

/** Map an HTTP status + server `detail` blob onto a VOICE_ERRORS code. */
export function classifyResponse(status, detail) {
  const serverCode = detail && typeof detail === 'object' ? detail.code : null;

  if (status === 401 || status === 403) return VOICE_ERRORS.NOT_AUTHENTICATED;
  if (status === 404) return VOICE_ERRORS.UNKNOWN;
  if (status === 503) {
    return serverCode === 'voice_provider_unavailable'
      ? VOICE_ERRORS.PROVIDER_UNAVAILABLE
      : VOICE_ERRORS.PROVIDER_UNAVAILABLE;
  }
  if (status === 502) return VOICE_ERRORS.PROVIDER_UNAVAILABLE;
  if (status === 400) {
    if (serverCode === 'empty_text') return VOICE_ERRORS.EMPTY_TRANSCRIPT;
    if (serverCode === 'invalid_audio') return VOICE_ERRORS.INIT_FAILED;
    return VOICE_ERRORS.UNKNOWN;
  }
  if (status >= 500) return VOICE_ERRORS.PROVIDER_UNAVAILABLE;
  return VOICE_ERRORS.UNKNOWN;
}

/** Read the JSON body without letting a malformed one throw. */
async function readDetail(response) {
  try {
    return await response.json();
  } catch (e) {
    return null;
  }
}

/**
 * @param {object} [options]
 * @param {function} [options.fetchImpl] injectable fetch (tests)
 */
export function createVoiceClient(options = {}) {
  const doFetch = options.fetchImpl || ((...args) => globalThis.fetch(...args));

  async function request(path, init = {}) {
    let response;
    try {
      response = await doFetch(API_PREFIX + path, {
        credentials: 'same-origin',
        ...init,
      });
    } catch (cause) {
      // No response at all: the backend is down or the network dropped.
      throw new VoiceApiError(VOICE_ERRORS.BACKEND_UNAVAILABLE, 'backend unreachable', {
        cause,
      });
    }

    if (!response.ok) {
      const body = await readDetail(response);
      const detail = body && body.detail;
      const code = classifyResponse(response.status, detail);
      const message =
        (detail && typeof detail === 'object' && detail.message) ||
        (typeof detail === 'string' ? detail : '') ||
        `voice request failed (${response.status})`;
      throw new VoiceApiError(code, message, { status: response.status });
    }
    return response;
  }

  async function json(path, init) {
    const response = await request(path, init);
    try {
      return await response.json();
    } catch (cause) {
      throw new VoiceApiError(VOICE_ERRORS.UNKNOWN, 'malformed voice response', {
        cause,
      });
    }
  }

  return {
    /** Capability report: which providers exist, what is enabled. */
    getSettings() {
      return json('/settings');
    },

    /** Start a session. `conversationId` ties it to an existing chat. */
    startSession({ conversationId = null, language = null, mode = 'push_to_talk', voice = null } = {}) {
      return json('/session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: conversationId,
          language,
          mode,
          voice,
        }),
      });
    },

    endSession(sessionId) {
      return json(`/session/${encodeURIComponent(sessionId)}/end`, { method: 'POST' });
    },

    /** Transcribe one recorded utterance. */
    async transcribe(sessionId, blob, filename = 'utterance.webm') {
      const form = new FormData();
      // The route declares `file`, so the field name matters.
      form.append('file', blob, filename);
      return json(`/session/${encodeURIComponent(sessionId)}/transcribe`, {
        method: 'POST',
        body: form,
      });
    },

    /** Synthesise text to audio bytes (used when the app TTS is unusable). */
    async synthesize(sessionId, text, { voice = null, speed = null, language = null } = {}) {
      const response = await request(`/session/${encodeURIComponent(sessionId)}/synthesize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, voice, speed, language }),
      });
      try {
        return await response.blob();
      } catch (cause) {
        throw new VoiceApiError(VOICE_ERRORS.TTS_UNAVAILABLE, 'no audio returned', {
          cause,
        });
      }
    },
  };
}
