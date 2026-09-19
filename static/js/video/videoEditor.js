// static/js/video/videoEditor.js
//
// The gallery's video editor.
//
// Two engines behind one UI, chosen at open time by asking the server what it
// can do (GET /api/video/capabilities):
//
//   * ffmpeg present → POST /api/video/{id}/edit and poll the job for real
//     progress. Fast, lossless where possible, and the only path that can
//     produce MP4 or GIF.
//   * ffmpeg absent  → encode in the browser with canvas + MediaRecorder. Real
//     time, WebM only, but the feature still works with nothing installed.
//
// The live preview is pure CSS (transform + clip-path), which is why rotate,
// flip, crop, speed and volume all respond instantly without touching the file.
//
// Crop is applied to the *source* frame, before rotation — exactly like the
// server's filter chain — so while the crop box is open the preview shows the
// unrotated source. That is a deliberate, labelled choice rather than a
// limitation: it makes the box mean what it says.

import {
  LIMITS,
  SPEED_CHOICES,
  clampOps,
  createOps,
  defaultCrop,
  formatLabel,
  formatTimecode,
  hasChanges,
  localExportNotice,
  opSummary,
  previewStyles,
  resizeCrop,
  toRequestPayload,
  trimRange,
} from './ops.js';
import {
  BrowserEncodeError,
  encodeLocally,
  isSupported as encoderSupported,
} from './browserEncoder.js';

const VIDEO_PATTERN = /\.(mp4|mov|webm|mkv|m4v)(\?|#|$)/i;

/** True for a URL that points at something the video editor can open. */
export function isVideoUrl(url) {
  return typeof url === 'string' && VIDEO_PATTERN.test(url);
}

/** Errors that should reach the user as a sentence, not a stack. */
function messageFor(error) {
  if (!error) return 'Something went wrong.';
  if (error.name === 'AbortError') return 'Export cancelled.';
  if (error instanceof BrowserEncodeError) return error.message;
  if (error.code === 'video_error' || error.code) {
    return error.detail ? `${error.message} — ${error.detail}` : error.message;
  }
  return error.message || 'Something went wrong.';
}

async function apiJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: 'same-origin',
    headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    // FastAPI wraps our structured failures in `detail`.
    const detail = payload?.detail;
    const structured = detail && typeof detail === 'object' ? detail : null;
    const error = new Error(
      structured?.error || (typeof detail === 'string' ? detail : '') ||
      `Request failed (${response.status})`
    );
    error.code = structured?.code || `http_${response.status}`;
    error.detail = structured?.detail || '';
    error.status = response.status;
    throw error;
  }
  return payload;
}

const HTML = `
<div class="video-editor-panel" role="dialog" aria-modal="true" aria-labelledby="video-editor-title">
  <header class="video-editor-head">
    <div class="video-editor-heading">
      <svg class="video-editor-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
      </svg>
      <h2 id="video-editor-title" class="video-editor-title">Edit video</h2>
    </div>
    <div class="video-editor-head-right">
      <span class="video-editor-engine" id="ve-engine" title="Where the export runs"></span>
      <button type="button" class="video-editor-close" id="ve-close" aria-label="Close video editor">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
  </header>

  <div class="video-editor-body">
    <div class="video-editor-main">
      <div class="video-editor-stage" id="ve-stage">
        <video id="ve-video" playsinline preload="metadata" controls></video>
        <div class="video-editor-crop-layer" id="ve-crop-layer" hidden>
          <div class="video-editor-crop-box" id="ve-crop-box">
            <span class="video-editor-crop-handle" data-edge="nw"></span>
            <span class="video-editor-crop-handle" data-edge="ne"></span>
            <span class="video-editor-crop-handle" data-edge="sw"></span>
            <span class="video-editor-crop-handle" data-edge="se"></span>
          </div>
        </div>
      </div>

      <div class="video-editor-timeline">
        <div class="video-editor-track" id="ve-track">
          <div class="video-editor-kept" id="ve-kept"></div>
          <div class="video-editor-playhead" id="ve-playhead"></div>
        </div>
        <div class="video-editor-tl-row">
          <label class="video-editor-tl-field">
            <span class="video-editor-tl-label">In</span>
            <input type="range" id="ve-in" min="0" max="0" step="0.05" value="0" aria-label="Trim start" />
            <output id="ve-in-readout" class="video-editor-tl-readout">0:00</output>
          </label>
          <label class="video-editor-tl-field">
            <span class="video-editor-tl-label">Out</span>
            <input type="range" id="ve-out" min="0" max="0" step="0.05" value="0" aria-label="Trim end" />
            <output id="ve-out-readout" class="video-editor-tl-readout">0:00</output>
          </label>
        </div>
        <div class="video-editor-tl-actions">
          <button type="button" class="video-editor-mini" id="ve-in-here">Set in at playhead</button>
          <button type="button" class="video-editor-mini" id="ve-out-here">Set out at playhead</button>
          <button type="button" class="video-editor-mini" id="ve-trim-reset">Reset trim</button>
          <span class="video-editor-tl-total" id="ve-total"></span>
        </div>
      </div>
    </div>

    <aside class="video-editor-side">
      <section class="video-editor-group">
        <h3 class="video-editor-group-title">Rotate &amp; flip</h3>
        <div class="video-editor-btn-row">
          <button type="button" class="video-editor-btn" id="ve-rot-ccw" title="Rotate 90° counter-clockwise">Rotate ↺</button>
          <button type="button" class="video-editor-btn" id="ve-rot-cw" title="Rotate 90° clockwise">Rotate ↻</button>
        </div>
        <div class="video-editor-btn-row">
          <button type="button" class="video-editor-btn" id="ve-flip-h" aria-pressed="false">Flip H</button>
          <button type="button" class="video-editor-btn" id="ve-flip-v" aria-pressed="false">Flip V</button>
        </div>
        <div class="video-editor-btn-row">
          <button type="button" class="video-editor-btn" id="ve-crop-toggle" aria-pressed="false">Crop</button>
          <button type="button" class="video-editor-btn" id="ve-crop-reset" hidden>Reset crop</button>
        </div>
        <p class="video-editor-note" id="ve-rotate-readout">No rotation</p>
      </section>

      <section class="video-editor-group">
        <h3 class="video-editor-group-title">Audio</h3>
        <label class="video-editor-check">
          <input type="checkbox" id="ve-mute" />
          <span>Remove audio</span>
        </label>
        <label class="video-editor-slider">
          <span>Volume <output id="ve-volume-readout">100%</output></span>
          <input type="range" id="ve-volume" min="0" max="4" step="0.05" value="1" />
        </label>
        <p class="video-editor-note" id="ve-audio-note"></p>
      </section>

      <section class="video-editor-group">
        <h3 class="video-editor-group-title">Speed</h3>
        <div class="video-editor-speed" id="ve-speed"></div>
      </section>

      <section class="video-editor-group">
        <h3 class="video-editor-group-title">Output</h3>
        <label class="video-editor-field">
          <span>Format</span>
          <select id="ve-format"></select>
        </label>
        <div class="video-editor-gif" id="ve-gif-fields" hidden>
          <label class="video-editor-field">
            <span>GIF frame rate <output id="ve-gif-fps-readout">12</output> fps</span>
            <input type="range" id="ve-gif-fps" min="1" max="30" step="1" value="12" />
          </label>
          <label class="video-editor-field">
            <span>GIF width <output id="ve-gif-width-readout">480</output> px</span>
            <input type="range" id="ve-gif-width" min="64" max="1280" step="16" value="480" />
          </label>
        </div>
        <label class="video-editor-field">
          <span>Save as</span>
          <select id="ve-save-mode">
            <option value="new">A new gallery item</option>
            <option value="replace">Replace the original</option>
          </select>
        </label>
        <label class="video-editor-field">
          <span>Name</span>
          <input type="text" id="ve-name" placeholder="Untitled video" maxlength="200" />
        </label>
      </section>

      <section class="video-editor-group video-editor-summary-group">
        <h3 class="video-editor-group-title">Changes</h3>
        <ul class="video-editor-summary" id="ve-summary"></ul>
      </section>
    </aside>
  </div>

  <div class="video-editor-progress" id="ve-progress" hidden>
    <div class="video-editor-bar"><div class="video-editor-bar-fill" id="ve-bar"></div></div>
    <div class="video-editor-progress-text">
      <span id="ve-progress-label">Working…</span>
      <span id="ve-progress-pct">0%</span>
    </div>
  </div>

  <footer class="video-editor-foot">
    <div class="video-editor-status" id="ve-status" role="status" aria-live="polite"></div>
    <div class="video-editor-foot-actions">
      <button type="button" class="video-editor-action" id="ve-frame" title="Save the current frame as a photo">Extract frame</button>
      <button type="button" class="video-editor-action" id="ve-cancel">Cancel</button>
      <button type="button" class="video-editor-action primary" id="ve-export">Export video</button>
    </div>
  </footer>
</div>
`;

class VideoEditor {
  constructor() {
    this.modal = null;
    this.state = null;
    this.caps = null;
    this.abort = null;
    this.cropDrag = null;
    this.onSaved = null;
    this.onFrame = null;
  }

  // ---------------------------------------------------------------- lifecycle

  async open({ id, url, name, onSaved, onFrame }) {
    this.onSaved = onSaved || null;
    this.onFrame = onFrame || null;
    this._ensureModal();

    this.state = {
      id,
      url,
      name: name || '',
      duration: 0,
      videoWidth: 0,
      videoHeight: 0,
      ops: createOps({ name: name || '' }),
      cropMode: false,
      busy: false,
    };

    this.modal.hidden = false;
    this.modal.classList.add('open');
    this._renderAll();
    this._setStatus('');

    const video = this._q('ve-video');
    video.src = url;

    // Capabilities first: it decides which engine the export uses, and whether
    // the format list offers MP4/GIF at all.
    try {
      this.caps = await apiJson('/api/video/capabilities?refresh=true');
    } catch {
      this.caps = { server_editing: false, ffmpeg: false, install_hint: '' };
    }
    this._renderEngine();

    try {
      const info = await apiJson(`/api/video/${encodeURIComponent(id)}/info`);
      this.state.duration = info.duration || 0;
      this.state.videoWidth = info.width || 0;
      this.state.videoHeight = info.height || 0;
      this.state.hasAudio = Boolean(info.has_audio);
      this._renderTimelineBounds();
      this._renderAudioNote();
    } catch (error) {
      // ffprobe may be missing while ffmpeg is present, and the <video>
      // element can still tell us the duration. Degrade, don't fail.
      this._setStatus(messageFor(error), 'warn');
      this._readDurationFromElement();
    }
  }

  close() {
    if (this.abort) this.abort.abort();
    this._stopBusy();
    const video = this._q('ve-video');
    if (video) {
      try { video.pause(); } catch { /* already paused */ }
      video.removeAttribute('src');
      try { video.load(); } catch { /* ignore */ }
    }
    if (this.modal) {
      this.modal.hidden = true;
      this.modal.classList.remove('open');
    }
    this.state = null;
    this._cancelCropDrag();
  }

  // ------------------------------------------------------------------- modal

  _ensureModal() {
    if (this.modal) return;
    const modal = document.createElement('div');
    modal.className = 'video-editor-modal';
    modal.id = 'video-editor-modal';
    modal.hidden = true;
    modal.innerHTML = HTML;
    document.body.appendChild(modal);
    this.modal = modal;

    this._q('ve-close').addEventListener('click', () => this.close());
    this._q('ve-cancel').addEventListener('click', () => this.close());
    this._q('ve-export').addEventListener('click', () => this.export());
    this._q('ve-frame').addEventListener('click', () => this.extractFrame());

    modal.addEventListener('mousedown', (event) => {
      if (event.target === modal) this.close();
    });
    modal.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !this.state?.busy) {
        event.stopPropagation();
        this.close();
      }
    });

    const set = (key, value) => this._patch({ [key]: value });

    this._q('ve-rot-ccw').addEventListener('click', () =>
      set('rotate', (((this.state.ops.rotate - 90) % 360) + 360) % 360));
    this._q('ve-rot-cw').addEventListener('click', () =>
      set('rotate', (this.state.ops.rotate + 90) % 360));
    this._q('ve-flip-h').addEventListener('click', () =>
      set('flipH', !this.state.ops.flipH));
    this._q('ve-flip-v').addEventListener('click', () =>
      set('flipV', !this.state.ops.flipV));

    this._q('ve-crop-toggle').addEventListener('click', () => {
      const turningOn = !this.state.cropMode;
      this._patch({
        cropMode: turningOn,
        // Opening the box seeds a sensible default so there is something to drag.
        crop: turningOn && !this.state.ops.crop ? defaultCrop() : this.state.ops.crop,
      });
      if (!turningOn) this._cancelCropDrag();
    });
    this._q('ve-crop-reset').addEventListener('click', () => this._patch({ crop: null }));

    this._q('ve-mute').addEventListener('change', (event) =>
      this._patch({ mute: event.target.checked }));
    this._q('ve-volume').addEventListener('input', (event) =>
      this._patch({ volume: Number(event.target.value) }));

    this._q('ve-in').addEventListener('input', (event) =>
      this._patch({ trimStart: Number(event.target.value) }));
    this._q('ve-out').addEventListener('input', (event) =>
      this._patch({ trimEnd: Number(event.target.value) }));

    const video = this._q('ve-video');
    this._q('ve-in-here').addEventListener('click', () =>
      this._patch({ trimStart: Math.min(video.currentTime, this.state.ops.trimEnd ?? Infinity) }));
    this._q('ve-out-here').addEventListener('click', () =>
      this._patch({ trimEnd: Math.max(video.currentTime, this.state.ops.trimStart) }));
    this._q('ve-trim-reset').addEventListener('click', () =>
      this._patch({ trimStart: 0, trimEnd: null }));

    video.addEventListener('timeupdate', () => this._renderPlayhead());
    video.addEventListener('loadedmetadata', () => this._readDurationFromElement());

    this._q('ve-format').addEventListener('change', (event) =>
      this._patch({ outputFormat: event.target.value }));
    this._q('ve-save-mode').addEventListener('change', (event) =>
      this._patch({ saveMode: event.target.value }));
    this._q('ve-name').addEventListener('input', (event) =>
      this._patch({ name: event.target.value }, { rerenderSummary: false }));
    this._q('ve-gif-fps').addEventListener('input', (event) =>
      this._patch({ gifFps: Number(event.target.value) }));
    this._q('ve-gif-width').addEventListener('input', (event) =>
      this._patch({ gifWidth: Number(event.target.value) }));

    this._wireCropDrag();
  }

  _q(id) {
    return this.modal.querySelector(`#${id}`);
  }

  _patch(changes, { rerenderSummary = true } = {}) {
    if (!this.state || this.state.busy) return;
    const next = { ...this.state, ...changes };
    next.ops = clampOps(next.ops);
    this.state = next;
    this._renderAll({ rerenderSummary });
  }

  // ---------------------------------------------------------------- rendering

  _renderAll({ rerenderSummary = true } = {}) {
    if (!this.state) return;
    this._renderToggleStates();
    this._renderPreview();
    this._renderTrim();
    this._renderAudio();
    this._renderSpeed();
    this._renderFormat();
    if (rerenderSummary) this._renderSummary();
    this._renderExportEnabled();
  }

  _renderEngine() {
    const el = this._q('ve-engine');
    if (!el) return;
    if (this.caps?.server_editing) {
      el.textContent = `Server (ffmpeg ${this.caps.version || ''})`.trim();
      el.dataset.engine = 'server';
      el.title = 'Exports run on the server: fast, lossless where possible, any format.';
    } else if (encoderSupported()) {
      el.textContent = 'Browser encoder';
      el.dataset.engine = 'browser';
      el.title = this.caps?.install_hint
        ? `No ffmpeg on the server, so your browser will encode. ${this.caps.install_hint}`
        : 'No ffmpeg on the server, so your browser will encode in real time (WebM only).';
    } else {
      el.textContent = 'Unavailable';
      el.dataset.engine = 'none';
    }
    this._renderFormat();
  }

  _renderToggleStates() {
    const { ops, cropMode } = this.state;
    this._q('ve-flip-h').setAttribute('aria-pressed', String(ops.flipH));
    this._q('ve-flip-v').setAttribute('aria-pressed', String(ops.flipV));
    this._q('ve-crop-toggle').setAttribute('aria-pressed', String(cropMode));
    this._q('ve-crop-toggle').classList.toggle('active', cropMode);
    this._q('ve-flip-h').classList.toggle('active', ops.flipH);
    this._q('ve-flip-v').classList.toggle('active', ops.flipV);
    this._q('ve-crop-reset').hidden = !ops.crop;
    this._q('ve-rotate-readout').textContent = ops.rotate
      ? `Rotated ${ops.rotate}°${ops.flipH ? ', flipped horizontally' : ''}${ops.flipV ? ', flipped vertically' : ''}`
      : 'No rotation';
    this._q('ve-mute').checked = ops.mute;
    this._q('ve-volume').value = String(ops.volume);
    this._q('ve-volume').disabled = ops.mute;
  }

  _renderPreview() {
    const { ops, cropMode } = this.state;
    const video = this._q('ve-video');
    const stage = this._q('ve-stage');

    // While the crop box is open, show the *source* frame so the box means what
    // it says — crop is applied before rotation on the server too.
    const previewOps = cropMode ? { ...ops, rotate: 0, flipH: false, flipV: false } : ops;
    const styles = previewStyles(previewOps, {
      width: this.state.videoWidth,
      height: this.state.videoHeight,
    });

    video.style.transform = styles.transform;
    video.style.clipPath = cropMode ? 'none' : styles.clipPath;
    if (stage && styles.aspectRatio) stage.style.aspectRatio = styles.aspectRatio;
    try {
      video.playbackRate = styles.playbackRate;
      video.muted = styles.muted;
      // Never audition >100% volume at full blast while scrubbing.
      video.volume = Math.min(1, styles.volume);
    } catch { /* the element may not be ready yet */ }

    const layer = this._q('ve-crop-layer');
    layer.hidden = !cropMode && !ops.crop;
    layer.classList.toggle('interactive', cropMode);
    if (ops.crop) this._renderCropBox(ops.crop);
  }

  _renderCropBox(crop) {
    const box = this._q('ve-crop-box');
    box.style.left = `${crop.x * 100}%`;
    box.style.top = `${crop.y * 100}%`;
    box.style.width = `${crop.width * 100}%`;
    box.style.height = `${crop.height * 100}%`;
  }

  _renderTimelineBounds() {
    const total = this.state.duration || 0;
    for (const id of ['ve-in', 've-out']) {
      const input = this._q(id);
      input.max = String(total || 0);
      input.step = total > 0 ? String(Math.max(0.05, total / 500)) : '0.05';
    }
    this._q('ve-total').textContent = total ? `${formatTimecode(total)} total` : '';
  }

  _readDurationFromElement() {
    if (!this.state) return;
    const video = this._q('ve-video');
    if (!this.state.duration && Number.isFinite(video.duration) && video.duration > 0) {
      this.state.duration = video.duration;
      this.state.videoWidth = this.state.videoWidth || video.videoWidth;
      this.state.videoHeight = this.state.videoHeight || video.videoHeight;
      this._renderTimelineBounds();
      this._renderTrim();
      this._renderSummary();
    }
  }

  _renderTrim() {
    const { ops } = this.state;
    const range = trimRange(ops, this.state.duration);
    const total = this.state.duration || 0;

    const inInput = this._q('ve-in');
    const outInput = this._q('ve-out');
    // Only move a handle the user is not currently dragging.
    if (document.activeElement !== inInput) inInput.value = String(range.start);
    if (document.activeElement !== outInput) {
      outInput.value = String(ops.trimEnd === null ? total : range.end);
    }
    this._q('ve-in-readout').textContent = formatTimecode(range.start);
    this._q('ve-out-readout').textContent = formatTimecode(range.end);

    const kept = this._q('ve-kept');
    if (total > 0) {
      kept.style.left = `${(range.start / total) * 100}%`;
      kept.style.width = `${(range.length / total) * 100}%`;
    } else {
      kept.style.left = '0%';
      kept.style.width = '100%';
    }
    this._renderPlayhead();
  }

  _renderPlayhead() {
    const video = this._q('ve-video');
    const playhead = this._q('ve-playhead');
    const total = this.state?.duration || 0;
    if (!video || !playhead || !total) return;
    const fraction = Math.min(1, Math.max(0, (video.currentTime || 0) / total));
    playhead.style.left = `${fraction * 100}%`;
  }

  _renderAudio() {
    const { ops } = this.state;
    this._q('ve-volume-readout').textContent = `${Math.round(ops.volume * 100)}%`;
    const note = this._q('ve-audio-note');
    if (this.state.hasAudio === false) {
      note.textContent = 'This video has no audio track.';
    } else if (ops.mute) {
      note.textContent = 'Audio will be removed entirely.';
    } else {
      note.textContent = '';
    }
  }

  _renderSpeed() {
    const host = this._q('ve-speed');
    if (host.dataset.built !== '1') {
      host.innerHTML = SPEED_CHOICES
        .map((speed) => `<button type="button" class="video-editor-speed-btn" data-speed="${speed}" aria-pressed="false">${speed}×</button>`)
        .join('');
      host.dataset.built = '1';
      host.addEventListener('click', (event) => {
        const button = event.target.closest('[data-speed]');
        if (!button) return;
        this._patch({ speed: Number(button.dataset.speed) });
      });
    }
    host.querySelectorAll('[data-speed]').forEach((button) => {
      const active = Number(button.dataset.speed) === this.state.ops.speed;
      button.setAttribute('aria-pressed', String(active));
      button.classList.toggle('active', active);
    });
  }

  _renderFormat() {
    const select = this._q('ve-format');
    const serverCan = Boolean(this.caps?.server_editing);
    const current = this.state?.ops.outputFormat || 'same';
    const options = LIMITS.formats.filter(
      (format) => serverCan || format === 'same' || format === 'webm'
    );
    const wanted = options.includes(current) ? current : 'same';

    if (select.dataset.options !== options.join(',')) {
      select.innerHTML = options
        .map((format) => `<option value="${format}">${formatLabel(format)}</option>`)
        .join('');
      select.dataset.options = options.join(',');
    }
    select.value = wanted;
    if (!serverCan && current !== wanted) {
      // The user had picked MP4 (or GIF) and the engine changed underneath them.
      this.state.ops = clampOps({ ...this.state.ops, outputFormat: wanted });
      this._renderSummary();
    }

    const gif = this.state.ops.outputFormat === 'gif';
    this._q('ve-gif-fields').hidden = !gif;
    this._q('ve-gif-fps').value = String(this.state.ops.gifFps);
    this._q('ve-gif-fps-readout').textContent = String(this.state.ops.gifFps);
    this._q('ve-gif-width').value = String(this.state.ops.gifWidth);
    this._q('ve-gif-width-readout').textContent = String(this.state.ops.gifWidth);

    const saveSelect = this._q('ve-save-mode');
    saveSelect.value = this.state.ops.saveMode;
    const browserOnly = !serverCan;
    // A browser export cannot replace in place: the media route serves
    // `immutable` cache headers, so overwriting bytes would leave browsers
    // showing the old clip. Say so instead of silently doing the wrong thing.
    const replaceOption = saveSelect.querySelector('option[value="replace"]');
    if (replaceOption) {
      replaceOption.disabled = browserOnly;
      replaceOption.textContent = browserOnly
        ? 'Replace the original (needs ffmpeg)'
        : 'Replace the original';
    }
    if (browserOnly && this.state.ops.saveMode === 'replace') {
      this.state.ops = clampOps({ ...this.state.ops, saveMode: 'new' });
      saveSelect.value = 'new';
    }
  }

  _renderSummary() {
    const list = this._q('ve-summary');
    const lines = opSummary(this.state.ops, this.state.duration);
    if (!lines.length) {
      list.innerHTML = '<li class="video-editor-summary-empty">No changes yet — pick an option to start.</li>';
    } else {
      list.innerHTML = lines
        .map((line) => `<li><span class="video-editor-summary-label">${line.label}</span><span>${line.value}</span></li>`)
        .join('');
    }
  }

  _renderExportEnabled() {
    const exportBtn = this._q('ve-export');
    const frameBtn = this._q('ve-frame');
    const ready = Boolean(this.state && !this.state.busy);
    exportBtn.disabled = !ready || !hasChanges(this.state.ops);
    exportBtn.title = hasChanges(this.state.ops)
      ? 'Encode this edit'
      : 'Change something first';
    frameBtn.disabled = !ready;
  }

  _setStatus(text, kind = 'info') {
    const el = this._q('ve-status');
    if (!el) return;
    el.textContent = text || '';
    el.dataset.kind = kind;
  }

  // ------------------------------------------------------------- crop drag

  _wireCropDrag() {
    const box = this._q('ve-crop-box');
    const layer = this._q('ve-crop-layer');

    const begin = (event, edge) => {
      if (!this.state?.cropMode || this.state.busy) return;
      if (event.button !== undefined && event.button !== 0) return;
      const frame = layer.getBoundingClientRect();
      if (!frame.width || !frame.height) return;
      const crop = this.state.ops.crop || defaultCrop();
      this.cropDrag = {
        edge,
        startX: event.clientX,
        startY: event.clientY,
        crop,
        frame,
        moved: false,
      };
      event.preventDefault();
      event.stopPropagation();
    };

    box.addEventListener('pointerdown', (event) => {
      if (event.target.dataset.edge) {
        begin(event, event.target.dataset.edge);
      } else {
        begin(event, 'move');
      }
      if (this.cropDrag) box.setPointerCapture?.(event.pointerId);
    });

    const move = (event) => {
      const drag = this.cropDrag;
      if (!drag || !this.state) return;
      // Fractions, so the box survives the preview being resized or scaled.
      const dx = (event.clientX - drag.startX) / drag.frame.width;
      const dy = (event.clientY - drag.startY) / drag.frame.height;
      if (Math.abs(dx) > 0.001 || Math.abs(dy) > 0.001) drag.moved = true;
      const next = resizeCrop(drag.crop, drag.edge, dx, dy);
      this.state.ops = clampOps({ ...this.state.ops, crop: next });
      this._renderCropBox(next);
      this._renderSummary();
      event.preventDefault();
    };

    const end = () => {
      if (!this.cropDrag) return;
      this.cropDrag = null;
      this._renderAll();
    };

    // Window-level only: pointer capture retargets the events to the box, but
    // they still bubble, so listening in one place avoids doing the work twice.
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);

    // Double-click resets to the default box — a cheap escape hatch.
    box.addEventListener('dblclick', () => {
      if (!this.state?.cropMode) return;
      this._patch({ crop: defaultCrop() });
    });
  }

  _cancelCropDrag() {
    this.cropDrag = null;
  }

  // ---------------------------------------------------------------- progress

  _showProgress(label) {
    this._q('ve-progress').hidden = false;
    this._q('ve-progress-label').textContent = label;
    this._setProgress(0);
  }

  _setProgress(fraction) {
    const pct = Math.round(Math.min(1, Math.max(0, fraction)) * 100);
    this._q('ve-bar').style.width = `${pct}%`;
    this._q('ve-progress-pct').textContent = `${pct}%`;
  }

  _hideProgress() {
    this._q('ve-progress').hidden = true;
  }

  _startBusy(label) {
    this.state.busy = true;
    this.state.ops = clampOps(this.state.ops);
    this._q('ve-export').disabled = true;
    this._q('ve-cancel').disabled = true;
    this._q('ve-frame').disabled = true;
    this.abort = new AbortController();
    this._showProgress(label);
    this._setStatus('');
  }

  _stopBusy() {
    if (this.state) this.state.busy = false;
    this.abort = null;
    const cancel = this._q('ve-cancel');
    if (cancel) cancel.disabled = false;
    if (this.state) this._renderExportEnabled();
    this._hideProgress();
  }

  // ---------------------------------------------------------------- export

  async export() {
    if (!this.state || this.state.busy) return;
    const { ops } = this.state;
    if (!hasChanges(ops)) return;

    if (this.caps?.server_editing) {
      await this._exportOnServer();
    } else {
      await this._exportInBrowser();
    }
  }

  async _exportOnServer() {
    const { ops, id } = this.state;
    this._startBusy('Uploading the edit…');

    try {
      const start = await apiJson(`/api/video/${encodeURIComponent(id)}/edit`, {
        method: 'POST',
        body: JSON.stringify(toRequestPayload(ops)),
      });
      const jobId = start.job_id;
      if (!jobId) throw new Error('The server did not return a job id');

      const result = await this._pollJob(jobId);
      this._stopBusy();
      this._setProgress(1);

      const label = result?.replaced ? 'Original replaced' : 'Saved as a new video';
      const seconds = result?.stream_copy
        ? 'lossless — no re-encode'
        : result?.elapsed_seconds ? `${result.elapsed_seconds}s` : '';
      this._setStatus(`${label}${seconds ? ` (${seconds})` : ''}`, 'ok');
      this._notifySaved(result);
    } catch (error) {
      this._stopBusy();
      if (error?.name === 'AbortError') {
        this._setStatus('Export cancelled.', 'warn');
        return;
      }
      this._setStatus(messageFor(error), 'error');
    }
  }

  async _pollJob(jobId) {
    const signal = this.abort?.signal;
    for (;;) {
      if (signal?.aborted) {
        const err = new Error('Export cancelled');
        err.name = 'AbortError';
        throw err;
      }
      const job = await apiJson(`/api/video/jobs/${encodeURIComponent(jobId)}`);
      this._setProgress(job.progress || 0);

      if (job.status === 'running' || job.status === 'queued') {
        this._q('ve-progress-label').textContent =
          job.message || (job.progress > 0 ? 'Encoding…' : 'Starting…');
        await new Promise((resolve) => setTimeout(resolve, 500));
        continue;
      }
      if (job.status === 'error') {
        const error = new Error(job.error || 'The export failed');
        error.code = job.code;
        throw error;
      }
      return job.result || {};
    }
  }

  async _exportInBrowser() {
    const { ops, url, duration } = this.state;
    const notice = localExportNotice(ops);
    if (notice) {
      this._setStatus(notice, 'warn');
      return;
    }

    this._startBusy('Encoding in your browser…');
    const startedAt = Date.now();

    try {
      const result = await encodeLocally({
        url,
        ops,
        duration,
        signal: this.abort?.signal,
        onProgress: (fraction) => {
          this._setProgress(fraction);
          const elapsed = (Date.now() - startedAt) / 1000;
          // The browser encodes in real time, so the remaining estimate is
          // genuinely just "how much is left", not a guess.
          const left = fraction > 0.01 ? Math.max(0, elapsed / fraction - elapsed) : 0;
          this._q('ve-progress-label').textContent =
            `Encoding in your browser… ${left > 1 ? `about ${Math.ceil(left)}s left` : ''}`.trim();
        },
      });

      this._q('ve-progress-label').textContent = 'Saving to the gallery…';
      const saved = await this._uploadBlob(result);

      this._stopBusy();
      this._setProgress(1);
      this._setStatus('Saved as a new video (browser-encoded WebM).', 'ok');
      this._notifySaved(saved);
    } catch (error) {
      this._stopBusy();
      if (error?.name === 'AbortError') {
        this._setStatus('Export cancelled.', 'warn');
        return;
      }
      this._setStatus(messageFor(error), 'error');
    }
  }

  async _uploadBlob({ blob, ext }) {
    const base = (this.state.ops.name || this.state.name || 'edited-video')
      .replace(/[^\w\-. ]+/g, '')
      .trim() || 'edited-video';
    const form = new FormData();
    form.append('file', blob, `${base}.${ext}`);

    const response = await fetch('/api/gallery/upload', {
      method: 'POST',
      body: form,
      credentials: 'same-origin',
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(payload?.detail || `Could not save the video (${response.status})`);
    }
    return {
      gallery_id: payload.id,
      filename: payload.filename,
      url: payload.url || `/api/generated-image/${payload.filename}`,
      replaced: false,
      duplicate: Boolean(payload.duplicate),
    };
  }

  _notifySaved(result) {
    if (typeof this.onSaved === 'function') {
      try { this.onSaved(result); } catch { /* a UI callback must not break the export */ }
    }
  }

  // ----------------------------------------------------------------- frames

  async extractFrame() {
    if (!this.state || this.state.busy) return;
    const video = this._q('ve-video');
    const at = Math.max(0, video.currentTime || 0);

    if (this.caps?.server_editing) {
      try {
        this._setStatus('Extracting frame…');
        const frame = await apiJson(`/api/video/${encodeURIComponent(this.state.id)}/frame`, {
          method: 'POST',
          body: JSON.stringify({ at }),
        });
        this._setStatus('Frame saved to the gallery.', 'ok');
        this._notifyFrame(frame);
        return;
      } catch (error) {
        this._setStatus(messageFor(error), 'error');
        return;
      }
    }

    // No ffmpeg: grab the frame from the element we already have. This needs no
    // server support at all, so frame extraction stays available regardless.
    try {
      this._setStatus('Extracting frame…');
      const blob = await this._frameFromElement(video);
      if (!blob) throw new Error('This frame could not be read');
      const saved = await this._uploadBlob({ blob, ext: 'png' });
      this._setStatus('Frame saved to the gallery.', 'ok');
      this._notifyFrame(saved);
    } catch (error) {
      this._setStatus(messageFor(error), 'error');
    }
  }

  _frameFromElement(video) {
    return new Promise((resolve) => {
      if (!video.videoWidth) { resolve(null); return; }
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const ctx = canvas.getContext('2d');
      try {
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      } catch {
        resolve(null);
        return;
      }
      canvas.toBlob((blob) => resolve(blob), 'image/png');
    });
  }

  _notifyFrame(frame) {
    if (typeof this.onFrame === 'function') {
      try { this.onFrame(frame); } catch { /* ignore */ }
    }
  }
}

const editor = new VideoEditor();

/** Open the gallery's video editor for one item. */
export function openVideoEditor(options) {
  return editor.open(options);
}

export function closeVideoEditor() {
  editor.close();
}

export default { openVideoEditor, closeVideoEditor, isVideoUrl };
