// static/js/video/ops.js
//
// The edit-operation model, as pure functions.
//
// Deliberately free of DOM, network and browser APIs so the interesting parts —
// what a legal trim range is, when a trim needs no re-encode, which edits the
// browser can do on its own, how the live preview is expressed in CSS — are all
// exercised under `node` in tests/test_video_frontend.py.
//
// The bounds mirror services/video/schemas.py. The server re-checks everything,
// so a drift here can only produce a nicer message, never a bad encode.

/** Every bound the editor enforces, in one place. */
export const LIMITS = {
  minSpeed: 0.25,
  maxSpeed: 4.0,
  minVolume: 0.0,
  maxVolume: 4.0,
  minGifFps: 1,
  maxGifFps: 30,
  minGifWidth: 64,
  maxGifWidth: 1920,
  formats: ['same', 'mp4', 'webm', 'gif', 'mkv'],
  saveModes: ['new', 'replace'],
  /** The smallest crop worth allowing, as a fraction of the frame. */
  minCropFraction: 0.02,
};

export const SPEED_CHOICES = [0.25, 0.5, 1, 1.5, 2, 3, 4];

/** Formats the browser's own encoder can produce (WebM, by definition). */
export const LOCAL_FORMATS = ['same', 'webm'];

const FORMAT_LABELS = {
  same: 'Keep original',
  mp4: 'MP4',
  webm: 'WebM',
  gif: 'Animated GIF',
  mkv: 'MKV',
};

export function formatLabel(format) {
  return FORMAT_LABELS[format] || format;
}

/** A blank edit: nothing changed, nothing lost. */
export function createOps(overrides = {}) {
  return clampOps({
    trimStart: 0,
    trimEnd: null,
    rotate: 0,
    flipH: false,
    flipV: false,
    crop: null,
    mute: false,
    volume: 1,
    speed: 1,
    outputFormat: 'same',
    gifFps: 12,
    gifWidth: 480,
    saveMode: 'new',
    name: '',
    ...overrides,
  });
}

function clampNumber(value, min, max, fallback) {
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function normaliseRotate(value) {
  const n = Number.isFinite(Number(value)) ? Math.round(Number(value) / 90) * 90 : 0;
  return ((n % 360) + 360) % 360;
}

/** True when a crop stays inside the frame and is not degenerate. */
export function isValidCrop(crop) {
  if (!crop) return false;
  const { x, y, width, height } = crop;
  if (![x, y, width, height].every((n) => Number.isFinite(n))) return false;
  if (width < LIMITS.minCropFraction || height < LIMITS.minCropFraction) return false;
  if (x < 0 || y < 0) return false;
  if (x + width > 1.000001 || y + height > 1.000001) return false;
  return true;
}

/**
 * Force an ops object into something the server and the preview both accept.
 *
 * Clamping rather than throwing is deliberate: the values come from sliders and
 * drags, where an out-of-range value is a normal intermediate state, not a bug.
 */
export function clampOps(ops) {
  const out = { ...ops };
  out.rotate = normaliseRotate(out.rotate);
  out.flipH = Boolean(out.flipH);
  out.flipV = Boolean(out.flipV);
  out.mute = Boolean(out.mute);
  out.volume = clampNumber(out.volume, LIMITS.minVolume, LIMITS.maxVolume, 1);
  out.speed = clampNumber(out.speed, LIMITS.minSpeed, LIMITS.maxSpeed, 1);
  out.gifFps = Math.round(clampNumber(out.gifFps, LIMITS.minGifFps, LIMITS.maxGifFps, 12));
  out.gifWidth = Math.round(clampNumber(out.gifWidth, LIMITS.minGifWidth, LIMITS.maxGifWidth, 480));

  out.trimStart = clampNumber(out.trimStart, 0, Number.MAX_SAFE_INTEGER, 0);
  if (out.trimEnd === null || out.trimEnd === undefined || out.trimEnd === '') {
    out.trimEnd = null;
  } else {
    const end = clampNumber(out.trimEnd, 0, Number.MAX_SAFE_INTEGER, null);
    out.trimEnd = end;
  }
  // A reversed or zero-length range is never intended; clear the end so the
  // clip runs to the natural end rather than exporting nothing.
  if (out.trimEnd !== null && out.trimEnd <= out.trimStart) out.trimEnd = null;

  out.crop = isValidCrop(out.crop) ? { ...out.crop } : null;
  out.outputFormat = LIMITS.formats.includes(out.outputFormat) ? out.outputFormat : 'same';
  out.saveMode = LIMITS.saveModes.includes(out.saveMode) ? out.saveMode : 'new';
  if (typeof out.name !== 'string') out.name = '';
  return out;
}

/**
 * The trim range actually applied to a video of `duration` seconds.
 *
 * Returns resolved `start`/`end`/`length` so no caller has to reason about a
 * null `trimEnd` meaning "to the end", and so a range that runs past the end of
 * a shorter-than-expected video is corrected rather than sent to the server.
 */
export function trimRange(ops, duration) {
  const total = Number.isFinite(duration) && duration > 0 ? duration : 0;
  const start = Math.min(Math.max(0, ops.trimStart || 0), total);
  let end = ops.trimEnd === null || ops.trimEnd === undefined ? total : ops.trimEnd;
  end = Math.min(Math.max(start, end), total);
  return { start, end, length: Math.max(0, end - start) };
}

/** True when the geometry, speed or volume must be re-encoded. */
export function needsReencode(ops) {
  const geometry = Boolean(ops.crop) || ops.rotate !== 0 || ops.flipH || ops.flipV;
  return geometry || Math.abs(ops.speed - 1) > 1e-9 || Math.abs(ops.volume - 1) > 1e-9;
}

/** True when the export can be a lossless stream copy (a bare trim or mute). */
export function isLossless(ops) {
  return !needsReencode(ops) && ops.outputFormat !== 'gif';
}

/** True when the edit would change nothing at all. */
export function hasChanges(ops) {
  return (
    Boolean(ops.crop) ||
    ops.rotate !== 0 ||
    ops.flipH ||
    ops.flipV ||
    ops.mute ||
    Math.abs(ops.volume - 1) > 1e-9 ||
    Math.abs(ops.speed - 1) > 1e-9 ||
    ops.trimStart > 0 ||
    ops.trimEnd !== null ||
    ops.outputFormat !== 'same'
  );
}

/**
 * Whether the browser fallback can honour this edit.
 *
 * It records WebM via MediaRecorder, so it cannot produce an MP4 or a GIF, and
 * it re-encodes in real time. Everything else it handles.
 */
export function canExportLocally(ops) {
  return LOCAL_FORMATS.includes(ops.outputFormat);
}

/** A sentence explaining the fallback's limits, or null when there are none. */
export function localExportNotice(ops) {
  if (canExportLocally(ops)) return null;
  return (
    `Without ffmpeg on the server, this browser can only export WebM. ` +
    `${formatLabel(ops.outputFormat)} needs ffmpeg — switch the format to WebM to continue.`
  );
}

/** Human-readable list of what the edit will do. */
export function opSummary(ops, duration) {
  const lines = [];
  const range = trimRange(ops, duration);
  if (ops.trimStart > 0 || ops.trimEnd !== null) {
    lines.push({
      label: 'Trim',
      value: `${formatTimecode(range.start)} → ${formatTimecode(range.end)} (${formatTimecode(range.length)})`,
    });
  }
  if (ops.rotate) lines.push({ label: 'Rotate', value: `${ops.rotate}° clockwise` });
  if (ops.flipH) lines.push({ label: 'Flip', value: 'horizontal' });
  if (ops.flipV) lines.push({ label: 'Flip', value: 'vertical' });
  if (ops.crop) {
    lines.push({
      label: 'Crop',
      value: `${percent(ops.crop.width)} × ${percent(ops.crop.height)} of the frame`,
    });
  }
  if (ops.mute) lines.push({ label: 'Audio', value: 'removed' });
  else if (Math.abs(ops.volume - 1) > 1e-9) {
    lines.push({ label: 'Volume', value: percent(ops.volume) });
  }
  if (Math.abs(ops.speed - 1) > 1e-9) lines.push({ label: 'Speed', value: `${ops.speed}×` });
  if (ops.outputFormat !== 'same') {
    lines.push({ label: 'Format', value: formatLabel(ops.outputFormat) });
  }
  return lines;
}

function percent(fraction) {
  return `${Math.round(fraction * 100)}%`;
}

/** Seconds as `m:ss` or `h:mm:ss`, for timeline readouts. */
export function formatTimecode(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  const whole = Math.floor(total);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  const pad = (n) => String(n).padStart(2, '0');
  if (hours > 0) return `${hours}:${pad(minutes)}:${pad(secs)}`;
  return `${minutes}:${pad(secs)}`;
}

/**
 * CSS that makes the *preview element* show the edit before anything is encoded.
 *
 * This is the whole reason the editor feels immediate: rotate and flip are a
 * transform, and the crop is a negative `clip-path` inset, so the user sees the
 * result of a change without waiting for an encode.
 */
export function previewStyles(ops, frame = null) {
  const transforms = [];
  if (ops.rotate) transforms.push(`rotate(${ops.rotate}deg)`);
  if (ops.flipH) transforms.push('scaleX(-1)');
  if (ops.flipV) transforms.push('scaleY(-1)');

  const styles = {
    transform: transforms.length ? transforms.join(' ') : 'none',
    clipPath: 'none',
    playbackRate: ops.speed,
    volume: ops.mute ? 0 : ops.volume,
    muted: ops.mute,
    rotate: ops.rotate,
  };

  if (ops.crop) {
    // inset(top right bottom left) as percentages of the *element* box.
    const top = ops.crop.y * 100;
    const right = (1 - (ops.crop.x + ops.crop.width)) * 100;
    const bottom = (1 - (ops.crop.y + ops.crop.height)) * 100;
    const left = ops.crop.x * 100;
    styles.clipPath = `inset(${top}% ${right}% ${bottom}% ${left}%)`;
  }

  if (frame && frame.width && frame.height) {
    // Rotating by a quarter turn swaps the box, so the stage needs the right
    // aspect ratio or the Preview tab letterboxes it twice.
    const swapped = ops.rotate === 90 || ops.rotate === 270;
    const w = ops.crop ? frame.width * ops.crop.width : frame.width;
    const h = ops.crop ? frame.height * ops.crop.height : frame.height;
    styles.aspectRatio = swapped ? `${h} / ${w}` : `${w} / ${h}`;
  }

  return styles;
}

/** A sensible starting crop box: a centred 80% rectangle. */
export function defaultCrop() {
  return { x: 0.1, y: 0.1, width: 0.8, height: 0.8 };
}

/** Resize one crop edge while keeping the rectangle legal and inside frame. */
export function resizeCrop(crop, edge, dx, dy) {
  const min = LIMITS.minCropFraction;
  const next = { ...crop };

  if (edge === 'move') {
    next.x = Math.min(Math.max(0, crop.x + dx), 1 - crop.width);
    next.y = Math.min(Math.max(0, crop.y + dy), 1 - crop.height);
    return next;
  }

  if (edge.includes('w')) {
    const right = crop.x + crop.width;
    next.x = Math.min(Math.max(0, crop.x + dx), right - min);
    next.width = right - next.x;
  }
  if (edge.includes('e')) {
    next.width = Math.min(Math.max(min, crop.width + dx), 1 - crop.x);
  }
  if (edge.includes('n')) {
    const bottom = crop.y + crop.height;
    next.y = Math.min(Math.max(0, crop.y + dy), bottom - min);
    next.height = bottom - next.y;
  }
  if (edge.includes('s')) {
    next.height = Math.min(Math.max(min, crop.height + dy), 1 - crop.y);
  }
  return isValidCrop(next) ? next : crop;
}

/** The multipart/JSON body the server expects, named as its schema names them. */
export function toRequestPayload(ops) {
  return {
    trim_start: ops.trimStart,
    trim_end: ops.trimEnd,
    rotate: ops.rotate,
    flip_h: ops.flipH,
    flip_v: ops.flipV,
    crop: ops.crop
      ? { x: ops.crop.x, y: ops.crop.y, width: ops.crop.width, height: ops.crop.height }
      : null,
    mute: ops.mute,
    volume: ops.volume,
    speed: ops.speed,
    output_format: ops.outputFormat,
    gif_fps: ops.gifFps,
    gif_width: ops.gifWidth,
    save_mode: ops.saveMode,
    name: ops.name || null,
  };
}

/** Progress fraction from a wall-clock position, clamped for safety. */
export function progressFromPosition(position, range) {
  if (!range || !range.length) return 0;
  const done = (position - range.start) / range.length;
  if (!Number.isFinite(done)) return 0;
  return Math.min(1, Math.max(0, done));
}
