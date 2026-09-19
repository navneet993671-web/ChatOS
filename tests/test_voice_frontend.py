"""Frontend tests for the voice assistant modules.

Driven through `node --input-type=module`, the same technique
`tests/test_compare_js.py` uses: real JS execution, no bundler, no jsdom. If
`node` is not installed the suite skips itself.

The point of the stub-heavy tests here is that they exercise the parts a human
cannot easily test by hand — permission denial, an unavailable STT provider, an
empty transcript, barge-in — without a microphone or a browser.
"""

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None


@pytest.fixture(scope="module")
def node_available():
    if not _HAS_NODE:
        pytest.skip("node binary not on PATH")


def _run_node(script: str) -> dict:
    """Run a JS snippet and return the JSON logged by its last console.log."""
    res = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=_REPO,
        capture_output=True,
        timeout=30,
        text=True,
    )
    if res.returncode != 0:
        raise AssertionError(f"node failed:\n{res.stderr}\n{res.stdout}")
    out_lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if not out_lines:
        raise AssertionError("node produced no stdout")
    return json.loads(out_lines[-1])


# ── voiceTypes.js ────────────────────────────────────────────────────


def test_state_machine_rejects_illegal_transitions(node_available):
    """`speaking` must not be reachable straight from `idle`, and an unknown
    state must be treated as illegal rather than throwing — the overlay reads
    this table on every render."""
    script = textwrap.dedent("""
        const t = await import('./static/js/voice/voiceTypes.js');
        const seen = [];
        const m = t.createVoiceStateMachine(t.VOICE_STATES.IDLE, (next, prev) => seen.push(prev + '->' + next));
        m.set('speaking');            // illegal from idle: must be dropped
        const afterIllegal = m.state;
        m.set('listening');
        m.set('processing');
        m.set('speaking');
        m.set('idle');
        console.log(JSON.stringify({
          after_illegal: afterIllegal,
          can_idle_speaking: t.canTransition('idle', 'speaking'),
          can_listening_processing: t.canTransition('listening', 'processing'),
          can_error_from_listening: t.canTransition('listening', 'error'),
          can_unknown: t.canTransition('nonsense', 'idle'),
          sequence: seen,
          final: m.state,
        }));
    """)
    out = _run_node(script)
    assert out["after_illegal"] == "idle"
    assert out["can_idle_speaking"] is False
    assert out["can_listening_processing"] is True
    # Every path back to idle must exist, or a provider failure strands the UI.
    assert out["can_error_from_listening"] is True
    assert out["can_unknown"] is False
    assert out["sequence"] == [
        "idle->listening",
        "listening->processing",
        "processing->speaking",
        "speaking->idle",
    ]
    assert out["final"] == "idle"


def test_every_voice_error_has_a_message(node_available):
    """A code with no sentence would render as `undefined` in the overlay."""
    script = textwrap.dedent("""
        const t = await import('./static/js/voice/voiceTypes.js');
        const missing = Object.values(t.VOICE_ERRORS).filter(
          (code) => typeof t.VOICE_ERROR_MESSAGES[code] !== 'string' || !t.VOICE_ERROR_MESSAGES[code].trim()
        );
        console.log(JSON.stringify({
          count: Object.values(t.VOICE_ERRORS).length,
          missing,
          fallback: t.errorMessage('definitely-not-a-code'),
        }));
    """)
    out = _run_node(script)
    assert out["count"] >= 15
    assert out["missing"] == []
    assert out["fallback"]


def test_mic_errors_are_normalized(node_available):
    """getUserMedia only gives us a DOMException name; the UI needs a code."""
    script = textwrap.dedent("""
        const t = await import('./static/js/voice/voiceTypes.js');
        console.log(JSON.stringify([
          t.normalizeMicError({ name: 'NotAllowedError' }),
          t.normalizeMicError({ name: 'NotFoundError' }),
          t.normalizeMicError({ name: 'NotReadableError' }),
          t.normalizeMicError({ name: 'AbortError' }),
          t.normalizeMicError({ name: 'SomethingElse' }),
          t.normalizeMicError(null),
        ]));
    """)
    assert _run_node(script) == [
        "permission_denied",
        "no_microphone",
        "device_busy",
        "aborted",
        "init_failed",
        "init_failed",
    ]


def test_js_voice_states_match_backend_statuses():
    """The client and server must describe voice state with the same words.

    `services/voice/schemas.py` owns VOICE_STATUSES; `voiceTypes.js` mirrors it.
    Drift here means a server event names a state the overlay cannot render.
    """
    from services.voice.schemas import VOICE_STATUSES

    source = (Path("static/js/voice/voiceTypes.js")).read_text(encoding="utf-8")
    block = source.split("VOICE_STATES = Object.freeze({", 1)[1].split("})", 1)[0]
    js_states = set(re.findall(r"[A-Z_]+:\s*'([a-z_]+)'", block))

    assert js_states, "could not parse VOICE_STATES out of voiceTypes.js"
    missing = js_states - set(VOICE_STATUSES)
    assert not missing, f"JS states absent from the backend: {sorted(missing)}"


# ── voiceClient.js ───────────────────────────────────────────────────


def test_client_targets_voice_routes_without_sending_a_user_id(node_available):
    """Identity is the authenticated session, never a client-supplied id."""
    script = textwrap.dedent("""
        const { createVoiceClient } = await import('./static/js/voice/voiceClient.js');
        const calls = [];
        const client = createVoiceClient({
          fetchImpl: async (url, init) => {
            calls.push({ url, init });
            return { ok: true, status: 200, json: async () => ({ id: 'vs_1', text: 'hi' }) };
          },
        });
        await client.startSession({ conversationId: 'conv_9', mode: 'push_to_talk' });
        const body = JSON.parse(calls[0].init.body);
        await client.transcribe('vs_1', new Blob([new Uint8Array([1, 2, 3])]));
        const second = calls[1];
        console.log(JSON.stringify({
          session_url: calls[0].url,
          session_method: calls[0].init.method,
          credentials: calls[0].init.credentials,
          body_keys: Object.keys(body).sort(),
          has_user_id: Object.prototype.hasOwnProperty.call(body, 'user_id'),
          conversation_id: body.conversation_id,
          transcribe_url: second.url,
          transcribe_method: second.init.method,
          is_form: second.init.body instanceof FormData,
          has_file: !!(second.init.body && typeof second.init.body.get === 'function' && second.init.body.get('file')),
        }));
    """)
    out = _run_node(script)
    assert out["session_url"] == "/api/voice/session"
    assert out["session_method"] == "POST"
    assert out["credentials"] == "same-origin"
    assert out["has_user_id"] is False
    assert "user_id" not in out["body_keys"]
    assert out["conversation_id"] == "conv_9"
    assert out["transcribe_url"] == "/api/voice/session/vs_1/transcribe"
    assert out["transcribe_method"] == "POST"
    assert out["is_form"] is True
    assert out["has_file"] is True


def test_client_classifies_failures(node_available):
    """Each status must land on a code the overlay has a sentence for."""
    script = textwrap.dedent("""
        const { classifyResponse } = await import('./static/js/voice/voiceClient.js');
        console.log(JSON.stringify([
          classifyResponse(401, null),
          classifyResponse(403, null),
          classifyResponse(503, { code: 'voice_provider_unavailable' }),
          classifyResponse(502, null),
          classifyResponse(400, { code: 'empty_text' }),
          classifyResponse(400, { code: 'invalid_audio' }),
          classifyResponse(500, null),
        ]));
    """)
    assert _run_node(script) == [
        "not_authenticated",
        "not_authenticated",
        "provider_unavailable",
        "provider_unavailable",
        "empty_transcript",
        "init_failed",
        "provider_unavailable",
    ]


def test_client_surfaces_backend_and_provider_errors(node_available):
    """A dead backend and a missing provider are different messages, and
    neither may escape as an unhandled rejection."""
    script = textwrap.dedent("""
        const mod = await import('./static/js/voice/voiceClient.js');
        const offline = mod.createVoiceClient({
          fetchImpl: async () => { throw new Error('ECONNREFUSED'); },
        });
        let offlineCode = null;
        try { await offline.getSettings(); } catch (e) { offlineCode = e.code; }

        const provider = mod.createVoiceClient({
          fetchImpl: async () => ({
            ok: false, status: 503,
            json: async () => ({ detail: { code: 'voice_provider_unavailable', message: 'no stt installed' } }),
          }),
        });
        let providerCode = null;
        let providerMessage = null;
        let isVoiceApiError = false;
        try {
          await provider.getSettings();
        } catch (e) {
          providerCode = e.code;
          providerMessage = e.message;
          isVoiceApiError = e instanceof mod.VoiceApiError;
        }
        console.log(JSON.stringify({ offlineCode, providerCode, providerMessage, isVoiceApiError }));
    """)
    out = _run_node(script)
    assert out["offlineCode"] == "backend_unavailable"
    assert out["providerCode"] == "provider_unavailable"
    assert out["providerMessage"] == "no stt installed"
    assert out["isVoiceApiError"] is True


# ── audioPlayback.js ─────────────────────────────────────────────────


def test_playback_reuses_streaming_tts_and_never_clobbers_user_choice(node_available):
    """Voice mode turns `autoPlay` on and must turn it back off — unless the
    user had TTS Mode enabled for text chat already, in which case leaving it on
    is the correct behaviour."""
    script = textwrap.dedent("""
        const mod = await import('./static/js/voice/audioPlayback.js');

        const stopped = [];
        const serverWin = {
          aiTTSManager: {
            available: true, _provider: 'local', autoPlay: false,
            isPlaying: false, _processing: false,
            stop() { stopped.push('tts'); },
          },
          speechSynthesis: { cancel() { stopped.push('speech'); } },
        };
        const p = mod.createPlayback({ win: serverWin });
        const engine = p.resolveEngine();
        p.enableAutoplay();
        const afterEnable = serverWin.aiTTSManager.autoPlay;
        const owned = p.ownsAutoplay;
        p.stop();
        p.restoreAutoplay();
        const afterRestore = serverWin.aiTTSManager.autoPlay;

        const userWin = {
          aiTTSManager: {
            available: true, _provider: 'local', autoPlay: true,
            isPlaying: false, _processing: false, stop() {},
          },
        };
        const p2 = mod.createPlayback({ win: userWin });
        p2.enableAutoplay();
        const ownedByVoice = p2.ownsAutoplay;
        p2.restoreAutoplay();
        const userChoicePreserved = userWin.aiTTSManager.autoPlay;

        console.log(JSON.stringify({
          engine, afterEnable, owned, afterRestore, stopped,
          ownedByVoice, userChoicePreserved,
          none: mod.resolveEngine({ aiTTSManager: { available: false, _provider: 'disabled' } }),
          browser: mod.resolveEngine({
            aiTTSManager: { available: false, _provider: 'disabled' },
            speechSynthesis: {}, SpeechSynthesisUtterance: function () {},
          }),
        }));
    """)
    out = _run_node(script)
    assert out["engine"] == "server"
    assert out["afterEnable"] is True
    assert out["owned"] is True
    # Barge-in must silence every engine that could be making noise.
    assert out["stopped"] == ["tts", "speech"]
    assert out["afterRestore"] is False
    assert out["ownedByVoice"] is False
    assert out["userChoicePreserved"] is True
    assert out["none"] is None
    assert out["browser"] == "browser"


# ── browserStt.js ────────────────────────────────────────────────────


def test_browser_recognizer_accumulates_and_ignores_no_speech(node_available):
    """The fallback engine has to survive its own normal end-of-utterance
    errors without reporting a failure to the user."""
    script = textwrap.dedent("""
        const mod = await import('./static/js/voice/browserStt.js');
        let last = null;
        class FakeSR {
          constructor() { last = this; }
          start() { this.started = true; }
          stop() { this.stopped = true; if (this.onend) this.onend(); }
          abort() { this.aborted = true; }
        }
        const win = { SpeechRecognition: FakeSR, setTimeout, clearTimeout };

        const partials = [];
        const errors = [];
        const r = mod.createRecognizer({
          win, lang: 'en-GB',
          onPartial: (t) => partials.push(t),
          onError: (c) => errors.push(c),
        });
        const started = r.start();
        const lang = last.lang; // capture before r2 replaces `last`
        // The handlers live on the recognizer instance the module created.
        last.onresult({
          resultIndex: 0,
          results: [
            Object.assign([{ transcript: 'find my ' }], { isFinal: true }),
            Object.assign([{ transcript: 'meetings' }], { isFinal: false }),
          ],
        });
        last.onerror({ error: 'no-speech' });
        const text = await r.stop();

        const r2 = mod.createRecognizer({ win });
        r2.start();
        last.onerror({ error: 'not-allowed' }); // no onError supplied: must not throw
        r2.abort();

        console.log(JSON.stringify({
          available: mod.isAvailable(win),
          unavailable: mod.isAvailable({}),
          started, text, lang, partials, errors,
        }));
    """)
    out = _run_node(script)
    assert out["available"] is True
    assert out["unavailable"] is False
    assert out["started"] is True
    assert out["text"] == "find my"
    assert out["lang"] == "en-GB"
    assert out["partials"] == ["find my meetings"]
    assert out["errors"] == []


# ── audioCapture.js ──────────────────────────────────────────────────


def test_capture_preflight_and_hard_duration_cap(node_available):
    """Recording must never be able to run forever, and an insecure origin must
    be refused before the browser throws an opaque error."""
    script = textwrap.dedent("""
        const mod = await import('./static/js/voice/audioCapture.js');
        const okWin = { isSecureContext: true, navigator: { mediaDevices: { getUserMedia() {} } }, MediaRecorder: function () {} };

        const preInsecure = mod.preflight({ isSecureContext: false, navigator: okWin.navigator, MediaRecorder: okWin.MediaRecorder });
        const preUnsupported = mod.preflight({ isSecureContext: true, navigator: {}, MediaRecorder: undefined });
        const preOk = mod.preflight(okWin);

        let autoStops = 0;
        class FakeMR {
          constructor(stream, opts) {
            this.mimeType = (opts && opts.mimeType) || 'audio/webm';
            this.state = 'inactive';
            FakeMR.last = this;
          }
          start() { this.state = 'recording'; }
          stop() {
            this.state = 'inactive';
            if (this.ondataavailable) this.ondataavailable({ data: new Blob([new Uint8Array([1, 2, 3])]) });
            if (this.onstop) this.onstop();
          }
        }
        const track = { stopped: false, stop() { this.stopped = true; } };
        const stream = { getTracks: () => [track] };
        const win = {
          isSecureContext: true,
          MediaRecorder: Object.assign(FakeMR, { isTypeSupported: () => true }),
          navigator: { mediaDevices: { getUserMedia: async () => stream } },
          setTimeout, clearTimeout,
          Blob,
        };
        const cap = mod.createCapture({ win, maxDurationMs: 600000, onAutoStop: () => autoStops++ });
        await cap.start();
        const recordingWhileLive = cap.isRecording;
        const result = await cap.stop();

        const deniedWin = Object.assign({}, win, {
          navigator: {
            mediaDevices: {
              getUserMedia: async () => {
                const err = new Error('denied');
                err.name = 'NotAllowedError';
                throw err;
              },
            },
          },
        });
        let deniedCode = null;
        try { await mod.createCapture({ win: deniedWin }).start(); } catch (e) { deniedCode = e.code; }

        console.log(JSON.stringify({
          preInsecure, preUnsupported, preOk,
          recordingWhileLive,
          blob_size: result.blob ? result.blob.size : -1,
          is_blob: result.blob instanceof Blob,
          track_stopped: track.stopped,
          recording_after: cap.isRecording,
          deniedCode,
        }));
    """)
    out = _run_node(script)
    assert out["preInsecure"] == "insecure_context"
    assert out["preUnsupported"] == "unsupported"
    assert out["preOk"] is None
    assert out["recordingWhileLive"] is True
    assert out["is_blob"] is True
    assert out["blob_size"] == 3
    assert out["track_stopped"] is True
    # The mic must be released the moment recording ends.
    assert out["recording_after"] is False
    assert out["deniedCode"] == "permission_denied"


# ── chatBridge.js ────────────────────────────────────────────────────


def _composer_harness() -> str:
    """Shared JS: a fake composer that counts submits."""
    return textwrap.dedent("""
        function makeComposer({ initial = '', streaming = false } = {}) {
          const state = { submitted: 0 };
          const input = { value: initial, dispatchEvent() {}, focus() {} };
          const form = { requestSubmit() { state.submitted += 1; } };
          const sendBtn = { dataset: { mode: streaming ? 'streaming' : '' } };
          const doc = {
            getElementById: (id) => {
              if (id === 'message') return input;
              if (id === 'chat-form') return form;
              return null;
            },
            querySelector: (sel) => (sel === '.send-btn' ? sendBtn : null),
          };
          return { doc, input, sendBtn, state };
        }
    """)


def test_bridge_appends_and_submits_through_the_form(node_available):
    """A spoken follow-up must not discard a half-typed message, and it must
    reach the chat through the form so app.js's own submit hooks still run."""
    script = _composer_harness() + textwrap.dedent("""
        const bridge = await import('./static/js/voice/chatBridge.js');

        const a = makeComposer({ initial: 'remember to' });
        const submittedA = bridge.submitTranscript('buy milk', { doc: a.doc });
        const empty = bridge.submitTranscript('   ', { doc: a.doc });

        const b = makeComposer();
        bridge.submitTranscript('hello', { append: false, doc: b.doc });

        const history = {
          querySelectorAll: () => [
            { dataset: { raw: 'first reply' } },
            { dataset: { raw: 'second reply' } },
            { dataset: { raw: '   ' } },
          ],
        };
        const readDoc = { getElementById: (id) => (id === 'chat-history' ? history : null) };

        console.log(JSON.stringify({
          submittedA,
          value_after_append: a.input.value,
          submits: a.state.submitted,
          empty,
          replace_mode_value: b.input.value,
          last_assistant: bridge.readLastAssistantText(readDoc),
          no_history: bridge.readLastAssistantText({ getElementById: () => null }),
        }));
    """)
    out = _run_node(script)
    assert out["submittedA"] == "remember to buy milk"
    assert out["value_after_append"] == "remember to buy milk"
    # Only the real transcript submits; the whitespace-only call must not.
    assert out["submits"] == 1
    assert out["empty"] is None
    assert out["replace_mode_value"] == "hello"
    assert out["last_assistant"] == "second reply"
    assert out["no_history"] == ""


def test_bridge_reports_stream_state_from_the_send_button(node_available):
    """`data-mode` is how chat.js advertises a live stream; voice state follows it."""
    script = _composer_harness() + textwrap.dedent("""
        const bridge = await import('./static/js/voice/chatBridge.js');
        const idle = makeComposer();
        const busy = makeComposer({ streaming: true });
        console.log(JSON.stringify({
          idle: bridge.isStreaming(idle.doc),
          busy: bridge.isStreaming(busy.doc),
        }));
    """)
    out = _run_node(script)
    assert out["idle"] is False
    assert out["busy"] is True


# ── voiceAssistant.js: the loop ──────────────────────────────────────


def _assistant_harness() -> str:
    return _composer_harness() + textwrap.dedent("""
        function makeWin(overrides = {}) {
          class FakeMR {
            constructor(stream, opts) {
              this.mimeType = (opts && opts.mimeType) || 'audio/webm';
              this.state = 'inactive';
            }
            start() { this.state = 'recording'; }
            stop() {
              this.state = 'inactive';
              if (this.ondataavailable) this.ondataavailable({ data: new Blob([new Uint8Array([7])]) });
              if (this.onstop) this.onstop();
            }
          }
          const track = { stop() {} };
          const win = {
            isSecureContext: true,
            MediaRecorder: Object.assign(FakeMR, { isTypeSupported: () => true }),
            navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [track] }) } },
            setTimeout, clearTimeout,
            chatModule: { abortCurrentRequest() {} },
            sessionModule: { getCurrentSessionId: () => 'conv_1' },
          };
          return Object.assign(win, overrides);
        }

        function makePlayback(log) {
          return {
            resolveEngine: () => 'server',
            enableAutoplay: () => log.push('autoplay-on'),
            restoreAutoplay: () => log.push('autoplay-off'),
            isSpeaking: () => false,
            stop: () => log.push('stop'),
            speakFallback: async () => log.push('fallback'),
          };
        }
    """)


def test_push_to_talk_loop_transcribes_and_sends(node_available):
    """The whole point of the feature, minus the microphone: open the mic,
    transcribe, and hand the text to the existing chat composer — with speech
    armed BEFORE the submit so chat.js actually speaks the reply."""
    script = _assistant_harness() + textwrap.dedent("""
        const { createVoiceAssistant } = await import('./static/js/voice/voiceAssistant.js');

        const composer = makeComposer();
        const win = makeWin();
        const transcribed = [];
        const client = {
          getSettings: async () => ({
            enabled: true, stt_available: true, default_language: 'en', default_voice: '',
            max_recording_seconds: 120, silence_timeout_ms: 1200,
          }),
          startSession: async (args) => ({ id: 'vs_1', ...args }),
          transcribe: async (id, blob) => {
            transcribed.push({ id, size: blob.size });
            return { text: 'what is on my calendar tomorrow' };
          },
          endSession: async () => ({}),
        };
        const audioLog = [];
        const announced = [];
        const assistant = createVoiceAssistant({
          win, doc: composer.doc, client,
          playback: makePlayback(audioLog),
          announce: (e) => announced.push(e.code || e.level),
        });

        const settings = await assistant.init();
        const opened = await assistant.startListening();
        const stateWhileListening = assistant.state;
        const submitted = await assistant.finishListening();

        console.log(JSON.stringify({
          settings_enabled: settings.enabled,
          opened,
          stateWhileListening,
          submitted,
          input_value: composer.input.value,
          submits: composer.state.submitted,
          transcribed,
          audioLog,
          announced,
          session: assistant.currentSessionId(),
          state: assistant.state,
        }));
    """)
    out = _run_node(script)
    assert out["settings_enabled"] is True
    assert out["opened"] is True
    assert out["stateWhileListening"] == "listening"
    assert out["submitted"] == "what is on my calendar tomorrow"
    assert out["input_value"] == "what is on my calendar tomorrow"
    assert out["submits"] == 1
    assert out["transcribed"] == [{"id": "vs_1", "size": 1}]
    assert out["session"] == "vs_1"
    # Armed before submit, and only once.
    assert out["audioLog"] == ["autoplay-on"]
    assert out["announced"] == []
    assert out["state"] == "processing"


def test_loop_reports_missing_stt_provider_without_sending(node_available):
    """No server provider and no browser engine must produce an actionable
    error, not a silent dead microphone."""
    script = _assistant_harness() + textwrap.dedent("""
        const { createVoiceAssistant } = await import('./static/js/voice/voiceAssistant.js');
        const composer = makeComposer();
        const announced = [];
        const assistant = createVoiceAssistant({
          win: makeWin(),
          doc: composer.doc,
          client: {
            getSettings: async () => ({ enabled: true, stt_available: false }),
            startSession: async () => ({ id: 'vs_1' }),
            transcribe: async () => ({ text: '' }),
            endSession: async () => ({}),
          },
          playback: makePlayback([]),
          announce: (e) => announced.push(e.code),
        });
        await assistant.init();
        const opened = await assistant.startListening();
        console.log(JSON.stringify({ opened, state: assistant.state, announced, submits: composer.state.submitted }));
    """)
    out = _run_node(script)
    assert out["opened"] is False
    assert out["state"] == "error"
    assert out["announced"] == ["stt_unavailable"]
    assert out["submits"] == 0


def test_empty_transcript_is_reported_and_not_sent(node_available):
    """Silence must not post an empty message into the conversation."""
    script = _assistant_harness() + textwrap.dedent("""
        const { createVoiceAssistant } = await import('./static/js/voice/voiceAssistant.js');
        const composer = makeComposer();
        const announced = [];
        const assistant = createVoiceAssistant({
          win: makeWin(),
          doc: composer.doc,
          client: {
            getSettings: async () => ({ enabled: true, stt_available: true, max_recording_seconds: 120 }),
            startSession: async () => ({ id: 'vs_1' }),
            transcribe: async () => ({ text: '   ' }),
            endSession: async () => ({}),
          },
          playback: makePlayback([]),
          announce: (e) => announced.push(e.code),
        });
        await assistant.init();
        await assistant.startListening();
        const submitted = await assistant.finishListening();
        console.log(JSON.stringify({ submitted, state: assistant.state, announced, submits: composer.state.submitted }));
    """)
    out = _run_node(script)
    assert out["submitted"] is None
    assert out["state"] == "error"
    assert out["announced"] == ["empty_transcript"]
    assert out["submits"] == 0


def test_muting_replies_skips_speech_but_still_sends(node_available):
    """A muted user still gets their message through to the assistant."""
    script = _assistant_harness() + textwrap.dedent("""
        const { createVoiceAssistant } = await import('./static/js/voice/voiceAssistant.js');
        const composer = makeComposer();
        const audioLog = [];
        const assistant = createVoiceAssistant({
          win: makeWin(),
          doc: composer.doc,
          client: {
            getSettings: async () => ({ enabled: true, stt_available: true, max_recording_seconds: 120 }),
            startSession: async () => ({ id: 'vs_1' }),
            transcribe: async () => ({ text: 'hello' }),
            endSession: async () => ({}),
          },
          playback: makePlayback(audioLog),
          announce: () => {},
        });
        assistant.setSpeakReplies(false);
        audioLog.length = 0; // drop the 'stop' that muting itself issues
        await assistant.init();
        await assistant.startListening();
        await assistant.finishListening();
        const speechWhileMuted = audioLog.slice();
        assistant.setSpeakReplies(true);
        assistant.submitText('a second question');
        console.log(JSON.stringify({
          speechWhileMuted,
          afterUnmute: audioLog,
          submits: composer.state.submitted,
        }));
    """)
    out = _run_node(script)
    assert out["speechWhileMuted"] == []
    assert out["submits"] == 2
    assert out["afterUnmute"] == ["autoplay-on"]


def test_barge_in_stops_audio_and_cancels_generation(node_available):
    """Interruption has to silence playback AND stop the model run — otherwise
    the reply keeps generating in the background while the user talks."""
    script = _assistant_harness() + textwrap.dedent("""
        const { createVoiceAssistant } = await import('./static/js/voice/voiceAssistant.js');
        const composer = makeComposer({ streaming: true });
        const win = makeWin();
        const aborts = [];
        win.chatModule = { abortCurrentRequest: (flag) => aborts.push(flag) };
        const audioLog = [];
        const assistant = createVoiceAssistant({
          win, doc: composer.doc,
          client: { getSettings: async () => ({ enabled: true, stt_available: true }), endSession: async () => ({}) },
          playback: makePlayback(audioLog),
          announce: () => {},
        });
        await assistant.init();
        assistant.interrupt();

        // A second interrupt while nothing is streaming must not call abort.
        win.chatModule.abortCurrentRequest = (flag) => aborts.push(flag);
        composer.sendBtn.dataset.mode = '';
        assistant.interrupt();

        console.log(JSON.stringify({ aborts, audioLog, state: assistant.state }));
    """)
    out = _run_node(script)
    assert out["aborts"] == [True]
    assert out["audioLog"] == ["stop", "stop"]
    assert out["state"] == "idle"


def test_cancel_abandons_the_utterance_without_sending(node_available):
    """Escape must leave nothing behind: no submit, no transcript, no session."""
    script = _assistant_harness() + textwrap.dedent("""
        const { createVoiceAssistant } = await import('./static/js/voice/voiceAssistant.js');
        const composer = makeComposer();
        let ended = 0;
        const assistant = createVoiceAssistant({
          win: makeWin(),
          doc: composer.doc,
          client: {
            getSettings: async () => ({ enabled: true, stt_available: true, max_recording_seconds: 120 }),
            startSession: async () => ({ id: 'vs_1' }),
            endSession: async () => { ended += 1; },
          },
          playback: makePlayback([]),
          announce: () => {},
        });
        await assistant.init();
        await assistant.startListening();
        const listeningBeforeCancel = assistant.isListening;
        assistant.cancel();
        await new Promise((r) => setTimeout(r, 10));
        console.log(JSON.stringify({
          listeningBeforeCancel,
          listeningAfter: assistant.isListening,
          state: assistant.state,
          transcript: assistant.getSnapshot().transcript,
          submits: composer.state.submitted,
          session: assistant.currentSessionId(),
          ended,
        }));
    """)
    out = _run_node(script)
    assert out["listeningBeforeCancel"] is True
    assert out["listeningAfter"] is False
    assert out["state"] == "idle"
    assert out["transcript"] == ""
    assert out["submits"] == 0
    assert out["session"] is None
    assert out["ended"] == 1
