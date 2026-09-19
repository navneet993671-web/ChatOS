"""Frontend tests for the video editing modules.

Driven through `node --input-type=module`, the same technique
tests/test_voice_frontend.py and tests/test_compare_js.py use: real JS
execution, no bundler, no jsdom, and skipped when `node` is not installed.

The targets are the parts a human cannot check reliably by hand — clamp
behaviour at the bounds, trim maths on a short video, the CSS the live preview
generates, and the exact payload the server will validate.
"""

import json
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
    """Run a JS snippet and return the JSON logged by its last console.log.

    ``encoding`` is pinned to UTF-8 because node always writes UTF-8, while
    Python would otherwise decode this pipe with the Windows locale codec and
    turn `°` and `×` into mojibake.
    """
    res = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=_REPO,
        capture_output=True,
        timeout=60,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if res.returncode != 0:
        raise AssertionError(f"node failed:\n{res.stderr}\n{res.stdout}")
    out_lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if not out_lines:
        raise AssertionError("node produced no stdout")
    return json.loads(out_lines[-1])


# ── ops.js: defaults and clamping ────────────────────────────────────


def test_default_ops_change_nothing(node_available):
    """A freshly opened editor must not claim there is work to export."""
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const ops = o.createOps();
        console.log(JSON.stringify({
          ops,
          has_changes: o.hasChanges(ops),
          needs_reencode: o.needsReencode(ops),
          lossless: o.isLossless(ops),
          summary: o.opSummary(ops, 60),
        }));
    """)
    out = _run_node(script)
    assert out["has_changes"] is False
    assert out["needs_reencode"] is False
    # A bare export of the whole clip would be a lossless copy, not a re-encode.
    assert out["lossless"] is True
    assert out["summary"] == []


def test_values_are_clamped_into_the_servers_bounds(node_available):
    """Sliders produce out-of-range intermediate values; they must be tamed."""
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const wild = o.clampOps({
          speed: 99, volume: -5, gifFps: 900, gifWidth: 3,
          rotate: 450, outputFormat: 'avi', saveMode: 'obliterate',
          trimStart: -10, trimEnd: -1, name: 42,
        });
        console.log(JSON.stringify(wild));
    """)
    out = _run_node(script)
    assert out["speed"] == 4.0
    assert out["volume"] == 0.0
    assert out["gifFps"] == 30
    assert out["gifWidth"] == 64
    assert out["rotate"] == 90            # 450 normalises to a quarter turn
    assert out["outputFormat"] == "same"  # unknown format falls back
    assert out["saveMode"] == "new"       # never default into a destructive mode
    assert out["trimStart"] == 0
    # A reversed range is cleared so the clip runs to its natural end rather
    # than exporting zero frames.
    assert out["trimEnd"] is None
    assert out["name"] == ""


def test_rotation_normalises_in_both_directions(node_available):
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        console.log(JSON.stringify({
          neg: o.clampOps({ rotate: -90 }).rotate,
          neg270: o.clampOps({ rotate: -270 }).rotate,
          full: o.clampOps({ rotate: 360 }).rotate,
          odd: o.clampOps({ rotate: 45 }).rotate,
          three: o.clampOps({ rotate: 270 }).rotate,
        }));
    """)
    out = _run_node(script)
    assert out["neg"] == 270
    assert out["neg270"] == 90
    assert out["full"] == 0
    assert out["odd"] == 90   # 45 rounds to the nearest quarter turn
    assert out["three"] == 270


# ── ops.js: crop validity ────────────────────────────────────────────


def test_invalid_crops_are_dropped_not_kept(node_available):
    """A crop the server would reject must never survive into a request."""
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        console.log(JSON.stringify({
          overflows: o.clampOps({ crop: { x: 0.8, y: 0, width: 0.5, height: 0.5 } }).crop,
          negative: o.clampOps({ crop: { x: -0.1, y: 0, width: 0.5, height: 0.5 } }).crop,
          degenerate: o.clampOps({ crop: { x: 0, y: 0, width: 0, height: 0.5 } }).crop,
          nan: o.clampOps({ crop: { x: 'a', y: 0, width: 0.5, height: 0.5 } }).crop,
          full: o.clampOps({ crop: { x: 0, y: 0, width: 1, height: 1 } }).crop,
        }));
    """)
    out = _run_node(script)
    assert out["overflows"] is None
    assert out["negative"] is None
    assert out["degenerate"] is None
    assert out["nan"] is None
    assert out["full"] == {"x": 0, "y": 0, "width": 1, "height": 1}


def test_crop_edges_stay_inside_the_frame(node_available):
    """Dragging an edge past the frame must stop, not invert the rectangle."""
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const start = { x: 0.2, y: 0.2, width: 0.5, height: 0.5 };
        console.log(JSON.stringify({
          drag_right_too_far: o.resizeCrop(start, 'e', 10, 0),
          drag_left_too_far: o.resizeCrop(start, 'w', -10, 0),
          drag_up_too_far: o.resizeCrop(start, 'n', 0, -10),
          drag_down_too_far: o.resizeCrop(start, 's', 0, 10),
          move_way_off: o.resizeCrop(start, 'move', 5, 5),
          move_way_up: o.resizeCrop(start, 'move', -5, -5),
        }));
    """)
    out = _run_node(script)
    right = out["drag_right_too_far"]
    assert right["x"] == pytest.approx(0.2)
    assert right["x"] + right["width"] <= 1.000001

    left = out["drag_left_too_far"]
    assert left["x"] == 0
    assert left["width"] == pytest.approx(0.7)   # right edge held in place

    down = out["drag_down_too_far"]
    assert down["y"] + down["height"] <= 1.000001

    moved = out["move_way_off"]
    assert moved["x"] + moved["width"] <= 1.000001
    assert moved["y"] + moved["height"] <= 1.000001
    # And a move never changes the box size.
    assert moved["width"] == pytest.approx(0.5)

    up = out["move_way_up"]
    assert up["x"] == 0 and up["y"] == 0
    assert up["width"] == pytest.approx(0.5)


# ── ops.js: trim maths ───────────────────────────────────────────────


def test_trim_range_resolves_the_open_ended_case(node_available):
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        console.log(JSON.stringify({
          whole: o.trimRange(o.createOps(), 30),
          started: o.trimRange(o.createOps({ trimStart: 5 }), 30),
          bounded: o.trimRange(o.createOps({ trimStart: 5, trimEnd: 12 }), 30),
        }));
    """)
    out = _run_node(script)
    assert out["whole"] == {"start": 0, "end": 30, "length": 30}
    assert out["started"] == {"start": 5, "end": 30, "length": 25}
    assert out["bounded"] == {"start": 5, "end": 12, "length": 7}


def test_trim_range_survives_a_shorter_than_expected_video(node_available):
    """ffprobe can be missing and the element duration may be all we have."""
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        console.log(JSON.stringify({
          past_end: o.trimRange(o.createOps({ trimStart: 100, trimEnd: 200 }), 10),
          unknown: o.trimRange(o.createOps(), 0),
          zero_length: o.trimRange(o.createOps({ trimStart: 10, trimEnd: 10 }), 10),
        }));
    """)
    out = _run_node(script)
    past_end = out["past_end"]
    assert past_end["start"] <= 10
    assert past_end["length"] >= 0
    assert out["unknown"]["length"] == 0
    assert out["zero_length"]["length"] == 0


def test_progress_is_clamped(node_available):
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const range = { start: 5, end: 10, length: 5 };
        console.log(JSON.stringify([
          o.progressFromPosition(5, range),
          o.progressFromPosition(7.5, range),
          o.progressFromPosition(10, range),
          o.progressFromPosition(99, range),
          o.progressFromPosition(0, range),
          o.progressFromPosition(5, { start: 0, end: 0, length: 0 }),
        ]));
    """)
    out = _run_node(script)
    assert out == [0, 0.5, 1, 1, 0, 0]


# ── ops.js: which engine can do the job ──────────────────────────────


def test_lossless_path_is_recognised(node_available):
    """A bare trim and a mute both avoid a re-encode; anything visual does not."""
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const f = (over) => o.isLossless(o.clampOps({ ...o.createOps(), ...over }));
        console.log(JSON.stringify({
          trim: f({ trimStart: 1, trimEnd: 4 }),
          mute: f({ mute: true }),
          rotate: f({ rotate: 90 }),
          flip: f({ flipH: true }),
          crop: f({ crop: { x: 0, y: 0, width: 0.5, height: 0.5 } }),
          volume: f({ volume: 0.5 }),
          speed: f({ speed: 2 }),
          gif: f({ outputFormat: 'gif' }),
        }));
    """)
    out = _run_node(script)
    assert out["trim"] is True
    assert out["mute"] is True
    for key in ("rotate", "flip", "crop", "volume", "speed", "gif"):
        assert out[key] is False, key


def test_browser_fallback_refuses_formats_it_cannot_write(node_available):
    """MediaRecorder only writes WebM, and the UI has to say so up front."""
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const check = (fmt) => {
          const ops = o.clampOps({ ...o.createOps(), outputFormat: fmt });
          return { ok: o.canExportLocally(ops), notice: o.localExportNotice(ops) };
        };
        console.log(JSON.stringify({
          webm: check('webm'), same: check('same'),
          mp4: check('mp4'), gif: check('gif'),
        }));
    """)
    out = _run_node(script)
    assert out["webm"]["ok"] is True and out["webm"]["notice"] is None
    assert out["same"]["ok"] is True and out["same"]["notice"] is None
    assert out["mp4"]["ok"] is False
    assert "ffmpeg" in out["mp4"]["notice"]
    assert out["gif"]["ok"] is False


# ── ops.js: live preview ─────────────────────────────────────────────


def test_preview_styles_express_the_edit_as_css(node_available):
    """Rotate/flip are transforms and crop is a clip-path, so the preview is
    instant and needs no encode."""
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const crop = { x: 0.25, y: 0.1, width: 0.5, height: 0.4 };
        console.log(JSON.stringify({
          plain: o.previewStyles(o.createOps(), { width: 1920, height: 1080 }),
          rotated: o.previewStyles(o.clampOps({ rotate: 90 }), { width: 1920, height: 1080 }),
          flipped: o.previewStyles(o.clampOps({ flipH: true, flipV: true })),
          cropped: o.previewStyles(o.clampOps({ crop }), { width: 1000, height: 500 }),
          muted: o.previewStyles(o.clampOps({ mute: true, volume: 2 })),
          fast: o.previewStyles(o.clampOps({ speed: 2 })),
        }));
    """)
    out = _run_node(script)

    assert out["plain"]["transform"] == "none"
    assert out["plain"]["clipPath"] == "none"
    # A quarter turn swaps the stage's aspect ratio so the preview cannot
    # letterbox the rotated frame twice.
    assert out["plain"]["aspectRatio"] == "1920 / 1080"
    assert out["rotated"]["aspectRatio"] == "1080 / 1920"
    assert out["rotated"]["transform"] == "rotate(90deg)"

    assert out["flipped"]["transform"] == "scaleX(-1) scaleY(-1)"

    # inset(top right bottom left) as percentages of the element box.
    assert out["cropped"]["clipPath"] == "inset(10% 25% 50% 25%)"
    assert out["cropped"]["aspectRatio"] == "500 / 200"

    assert out["muted"]["muted"] is True
    assert out["muted"]["volume"] == 0
    assert out["fast"]["playbackRate"] == 2


def test_preview_volume_is_not_amplified_for_playback(node_available):
    """Volume above 100% is for the encode; audibling it would be painful.

    previewStyles reports the real value, and the editor clamps what it applies
    to the element — this asserts the reported value is honest so that clamp is
    meaningful.
    """
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        console.log(JSON.stringify(o.previewStyles(o.clampOps({ volume: 3 }))));
    """)
    out = _run_node(script)
    assert out["volume"] == 3


# ── ops.js: summary and formatting ───────────────────────────────────


def test_summary_describes_each_change(node_available):
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const ops = o.clampOps({
          trimStart: 5, trimEnd: 15, rotate: 90, flipH: true,
          crop: { x: 0, y: 0, width: 0.5, height: 0.25 },
          mute: true, speed: 2, outputFormat: 'webm',
        });
        console.log(JSON.stringify(o.opSummary(ops, 60)));
    """)
    out = _run_node(script)
    labels = [line["label"] for line in out]
    assert labels == ["Trim", "Rotate", "Flip", "Crop", "Audio", "Speed", "Format"]

    values = {line["label"]: line["value"] for line in out}
    assert "0:05" in values["Trim"] and "0:15" in values["Trim"]
    assert values["Rotate"] == "90° clockwise"
    assert values["Crop"] == "50% × 25% of the frame"
    assert values["Audio"] == "removed"
    assert values["Speed"] == "2×"
    # Mute beats volume — they cannot both appear.
    assert "Volume" not in values


def test_volume_shows_when_audio_is_kept(node_available):
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        console.log(JSON.stringify(o.opSummary(o.clampOps({ volume: 0.5 }), 10)));
    """)
    out = _run_node(script)
    assert out == [{"label": "Volume", "value": "50%"}]


def test_timecode_formatting(node_available):
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        console.log(JSON.stringify([
          o.formatTimecode(0), o.formatTimecode(9.9), o.formatTimecode(75),
          o.formatTimecode(3600), o.formatTimecode(3725), o.formatTimecode(-4),
        ]));
    """)
    out = _run_node(script)
    assert out == ["0:00", "0:09", "1:15", "1:00:00", "1:02:05", "0:00"]


# ── ops.js: the request the server will validate ─────────────────────


def test_payload_uses_the_servers_field_names(node_available):
    """A rename here would only surface as a 422 in the browser."""
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const ops = o.clampOps({
          trimStart: 1.5, trimEnd: 4, rotate: 270, flipH: true, flipV: false,
          crop: { x: 0.1, y: 0.2, width: 0.5, height: 0.6 },
          mute: false, volume: 0.75, speed: 1.5, outputFormat: 'mp4',
          gifFps: 15, gifWidth: 320, saveMode: 'new', name: 'clip',
        });
        console.log(JSON.stringify(o.toRequestPayload(ops)));
    """)
    out = _run_node(script)
    assert out == {
        "trim_start": 1.5,
        "trim_end": 4,
        "rotate": 270,
        "flip_h": True,
        "flip_v": False,
        "crop": {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.6},
        "mute": False,
        "volume": 0.75,
        "speed": 1.5,
        "output_format": "mp4",
        "gif_fps": 15,
        "gif_width": 320,
        "save_mode": "new",
        "name": "clip",
    }


def test_payload_with_no_trim_sends_a_null_end(node_available):
    """`null` means "to the end"; sending 0 would export nothing."""
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const payload = o.toRequestPayload(o.createOps());
        console.log(JSON.stringify({ end: payload.trim_end, crop: payload.crop, name: payload.name }));
    """)
    out = _run_node(script)
    assert out["end"] is None
    assert out["crop"] is None
    assert out["name"] is None


# ── browserEncoder.js ────────────────────────────────────────────────


def test_output_dimensions_swap_on_a_quarter_turn(node_available):
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const e = await import('./static/js/video/browserEncoder.js');
        const crop = { x: 0, y: 0, width: 0.5, height: 0.25 };
        console.log(JSON.stringify({
          plain: e.outputDimensions(1920, 1080, o.createOps()),
          rot90: e.outputDimensions(1920, 1080, o.clampOps({ rotate: 90 })),
          rot180: e.outputDimensions(1920, 1080, o.clampOps({ rotate: 180 })),
          cropped: e.outputDimensions(1000, 800, o.clampOps({ crop })),
          cropped_rot: e.outputDimensions(1000, 800, o.clampOps({ crop, rotate: 270 })),
        }));
    """)
    out = _run_node(script)
    assert out["plain"] == {"width": 1920, "height": 1080}
    assert out["rot90"] == {"width": 1080, "height": 1920}
    assert out["rot180"] == {"width": 1920, "height": 1080}
    assert out["cropped"] == {"width": 500, "height": 200}
    assert out["cropped_rot"] == {"width": 200, "height": 500}


def test_output_dimensions_are_always_even(node_available):
    """Odd dimensions are rejected by the encoders, so they must be rounded."""
    script = textwrap.dedent("""
        const o = await import('./static/js/video/ops.js');
        const e = await import('./static/js/video/browserEncoder.js');
        const odd = { x: 0, y: 0, width: 0.333, height: 0.777 };
        console.log(JSON.stringify([
          e.outputDimensions(101, 101, o.createOps()),
          e.outputDimensions(641, 361, o.clampOps({ rotate: 90 })),
          e.outputDimensions(999, 997, o.clampOps({ crop: odd })),
        ]));
    """)
    out = _run_node(script)
    for dims in out:
        assert dims["width"] % 2 == 0, dims
        assert dims["height"] % 2 == 0, dims
        assert dims["width"] >= 2 and dims["height"] >= 2


def test_encoder_reports_itself_unavailable_without_a_dom(node_available):
    """Under node there is no MediaRecorder; that must be a clean false, not a
    throw, because the editor calls this to choose its engine."""
    script = textwrap.dedent("""
        const e = await import('./static/js/video/browserEncoder.js');
        console.log(JSON.stringify({
          supported: e.isSupported(),
          mime: e.pickMimeType(),
        }));
    """)
    out = _run_node(script)
    assert out["supported"] is False
    assert out["mime"] is None


# ── wiring: the gallery and the service worker ───────────────────────


def test_video_modules_are_precached(node_available):
    """A module missing from the precache 404s offline at runtime."""
    sw = (_REPO / "static" / "sw.js").read_text(encoding="utf-8")
    for module in ("video/index.js", "video/ops.js",
                   "video/browserEncoder.js", "video/videoEditor.js"):
        assert f"/static/js/{module}" in sw, module


def test_gallery_routes_videos_to_the_video_editor():
    """The Edit button must open the video editor for a video, and the image
    rotate buttons must not be rendered for one (they call a PIL endpoint)."""
    source = (_REPO / "static" / "js" / "gallery.js").read_text(encoding="utf-8")
    assert "openVideoEditor" in source
    assert "isVideo ? _openVideoEditor : _openInEditor" in source
    # The rotate buttons live inside a conditional on `isVideo`.
    rotate_block = source[source.index("${isVideo ? '' :"):]
    assert "gallery-rotate-btn" in rotate_block[:600]


def test_editor_never_auditions_amplified_volume():
    """Volume above 100% is for the encode only; playing it back would clip."""
    source = (_REPO / "static" / "js" / "video" / "videoEditor.js").read_text(encoding="utf-8")
    assert "video.volume = Math.min(1, styles.volume)" in source


def test_browser_export_cannot_replace_in_place():
    """The media route serves immutable cache headers, so a browser export that
    overwrote bytes in place would keep showing the old clip. The option is
    disabled rather than silently ignored."""
    source = (_REPO / "static" / "js" / "video" / "videoEditor.js").read_text(encoding="utf-8")
    assert 'replaceOption.disabled = browserOnly' in source
