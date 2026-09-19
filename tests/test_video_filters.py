"""Tests for services/video/filters.py — pure argv and filter construction.

Deliberately no ffmpeg required: these assert the *command*, which is where the
expensive mistakes live (wrong filter order, a stream copy that silently drops
a user's rotation, an `-af` chain paired with `-an` that makes ffmpeg refuse to
run at all).
"""

import pytest

from services.video.filters import (
    VideoOps,
    VideoOpsError,
    atempo_chain,
    build_audio_filters,
    build_edit_args,
    build_frame_args,
    build_video_filters,
    can_stream_copy,
    container_family,
    resolve_container,
)


def _cmd(ops, *, ext="mp4", has_audio=True, stream_copy=False):
    """Build an argv and render it as one string for readable assertions."""
    args = build_edit_args(
        ffmpeg="ffmpeg", source=f"in.{ext}", destination="out.bin",
        ops=ops, source_ext=ext, source_has_audio=has_audio,
        stream_copy=stream_copy,
    )
    return " ".join(str(a) for a in args)


# --------------------------------------------------------------------- trim

def test_trim_uses_input_seeking_and_duration_not_to():
    """`-ss` must precede `-i` and the range must be expressed with `-t`.

    `-to` is interpreted against the post-seek timeline, so mixing the two
    silently produces the wrong range — the single most common way a trimmer
    ships off-by-N-seconds.
    """
    cmd = _cmd(VideoOps(trim_start=5.0, trim_end=15.0))
    assert cmd.index("-ss 5.000") < cmd.index("-i in.mp4")
    assert "-t 10.000" in cmd
    assert "-to" not in cmd


def test_no_trim_flags_when_nothing_is_trimmed():
    cmd = _cmd(VideoOps())
    assert "-ss" not in cmd
    assert "-t " not in cmd


def test_trim_end_alone_leaves_start_off_and_keeps_duration():
    cmd = _cmd(VideoOps(trim_end=4.0))
    assert "-ss" not in cmd
    assert "-t 4.000" in cmd


def test_trim_duration_property():
    assert VideoOps(trim_start=2.0, trim_end=7.5).trim_duration == 5.5
    assert VideoOps(trim_start=2.0).trim_duration is None
    # A reversed range is clamped to zero rather than going negative, so
    # progress maths can never divide by a negative length.
    assert VideoOps(trim_start=9.0, trim_end=3.0).trim_duration == 0.0


# ------------------------------------------------------------- stream copy

def test_pure_trim_stream_copies():
    """The most common request must not cost a re-encode."""
    assert can_stream_copy(VideoOps(trim_start=1.0, trim_end=2.0), "mp4") is True
    assert "-c:v copy" in _cmd(VideoOps(trim_start=1.0, trim_end=2.0), stream_copy=True)


def test_mute_still_stream_copies():
    """Dropping audio needs no encoder, so it must stay on the fast path."""
    assert can_stream_copy(VideoOps(mute=True), "mp4") is True


def test_any_visual_op_defeats_stream_copy():
    for ops in (
        VideoOps(rotate=90),
        VideoOps(flip_h=True),
        VideoOps(flip_v=True),
        VideoOps(crop=(0.1, 0.1, 0.5, 0.5)),
        VideoOps(speed=2.0),
        VideoOps(volume=0.5),
    ):
        assert can_stream_copy(ops, "mp4") is False, ops


def test_stream_copy_refused_when_container_changes():
    """`-c copy` cannot remux into a different container."""
    assert can_stream_copy(VideoOps(trim_end=2.0, output_format="webm"), "mp4") is False


def test_stream_copy_refused_for_gif():
    assert can_stream_copy(VideoOps(trim_end=2.0, output_format="gif"), "mp4") is False


def test_stream_copy_keeps_video_stream_untouched():
    cmd = _cmd(VideoOps(mute=True), stream_copy=True)
    assert "-c:v copy" in cmd
    assert "-an" in cmd
    # No filter may be present on a copied stream: ffmpeg rejects -vf with copy.
    assert "-vf" not in cmd
    assert "-af" not in cmd


# --------------------------------------------------------------- geometry

def test_crop_forces_even_dimensions_and_offsets():
    """h264/yuv420p rejects odd crop geometry, so the expression must round."""
    chain = build_video_filters(VideoOps(crop=(0.1, 0.2, 0.5, 0.25)), container="mp4")
    crop = chain[0]
    assert crop.startswith("crop=")
    # Four components, each divided by the frame and floored to an even number.
    assert crop.count("/2)*2") == 4
    assert "floor(iw*0.500000/2)*2" in crop   # width
    assert "floor(ih*0.250000/2)*2" in crop   # height
    assert "floor(iw*0.100000/2)*2" in crop   # x
    assert "floor(ih*0.200000/2)*2" in crop   # y


@pytest.mark.parametrize("rotate,expected", [
    (90, ["transpose=1"]),
    (270, ["transpose=2"]),
    (180, ["hflip", "vflip"]),
    (0, []),
])
def test_rotation_maps_to_the_right_transpose(rotate, expected):
    chain = build_video_filters(VideoOps(rotate=rotate), container="webm")
    assert [f for f in chain if "transpose" in f or "flip" in f] == expected


def test_crop_runs_before_rotation():
    """The user drew their box on the frame they were looking at (the source)."""
    chain = build_video_filters(
        VideoOps(crop=(0.0, 0.0, 0.5, 0.5), rotate=90), container="mp4"
    )
    assert chain[0].startswith("crop=")
    assert "transpose=1" in chain[1]


def test_flips_apply_after_rotation():
    chain = build_video_filters(VideoOps(rotate=90, flip_h=True), container="mp4")
    assert chain.index("transpose=1") < chain.index("hflip")


def test_geometry_chain_ends_with_even_scale_for_planar_codecs():
    chain = build_video_filters(VideoOps(rotate=90), container="mp4")
    assert chain[-1] == "scale=trunc(iw/2)*2:trunc(ih/2)*2"


def test_no_filters_at_all_for_a_plain_edit():
    assert build_video_filters(VideoOps(), container="mp4") == []


# ------------------------------------------------------------------- speed

def test_speed_rewrites_the_timeline():
    chain = build_video_filters(VideoOps(speed=2.0), container="mp4")
    assert "setpts=PTS/2.000000" in chain


@pytest.mark.parametrize("speed,expected_count", [
    (1.0, 0),
    (1.5, 1),
    (2.0, 1),
    (4.0, 2),
    (0.5, 1),
    (0.25, 2),
])
def test_atempo_chaining_respects_the_per_instance_limit(speed, expected_count):
    """A single atempo only spans 0.5–2.0, so larger jumps chain instances."""
    parts = atempo_chain(speed)
    assert len(parts) == expected_count
    if parts:
        product = 1.0
        for part in parts:
            product *= float(part.split("=")[1])
        assert product == pytest.approx(speed, rel=1e-6)


def test_atempo_rejects_nonpositive_speed():
    with pytest.raises(VideoOpsError):
        atempo_chain(0.0)


def test_gif_resamples_after_the_speed_change():
    """Frame-rate reduction must happen on the *new* timeline, not the old one."""
    chain = build_video_filters(VideoOps(speed=2.0, output_format="gif"), container="gif")
    assert chain.index("setpts=PTS/2.000000") < chain.index("fps=12")


# ------------------------------------------------------------------- audio

def test_volume_filter_only_when_it_changes():
    assert build_audio_filters(VideoOps()) == []
    assert build_audio_filters(VideoOps(volume=0.5)) == ["volume=0.5000"]


def test_volume_and_speed_combine_in_one_chain():
    assert build_audio_filters(VideoOps(volume=0.5, speed=2.0)) == [
        "volume=0.5000", "atempo=2.000000",
    ]


def test_mute_emits_no_audio_filter_chain():
    assert build_audio_filters(VideoOps(mute=True)) == []


def test_mute_is_an_an_flag_not_a_filter():
    cmd = _cmd(VideoOps(mute=True))
    assert "-an" in cmd
    assert "-af" not in cmd


def test_volume_change_on_a_silent_source_never_emits_af_with_an():
    """`-af` alongside `-an` makes ffmpeg refuse to run, so it must not appear.

    A source with no audio track cannot have its volume adjusted, and asking for
    it anyway must degrade to "drop audio", not to a failed job.
    """
    cmd = _cmd(VideoOps(volume=0.5), has_audio=False)
    assert "-af" not in cmd
    assert "-an" in cmd


def test_audio_only_edit_copies_the_video_stream():
    """Changing volume must not cost the video a generation of quality."""
    cmd = _cmd(VideoOps(volume=0.5))
    assert "-c:v copy" in cmd
    assert "libx264" not in cmd
    assert "-c:a aac" in cmd
    assert "-vf" not in cmd


def test_audio_only_edit_re_encodes_video_when_the_container_changes():
    """h264 is not legal inside webm, so a copy would produce a broken file."""
    cmd = _cmd(VideoOps(volume=0.5, output_format="webm"))
    assert "-c:v copy" not in cmd
    assert "libvpx-vp9" in cmd


def test_audio_only_edit_on_a_silent_source_still_copies_video():
    cmd = _cmd(VideoOps(volume=0.5), has_audio=False)
    assert "-c:v copy" in cmd
    assert "-c:a" not in cmd


# --------------------------------------------------------------------- gif

def test_gif_builds_a_single_pass_palette_graph():
    cmd = _cmd(VideoOps(output_format="gif"))
    assert "palettegen" in cmd
    assert "paletteuse" in cmd
    assert "fps=12" in cmd
    assert "-c:v gif" in cmd
    assert "-loop 0" in cmd
    assert "-an" in cmd


def test_gif_has_no_audio_filters_at_all():
    cmd = _cmd(VideoOps(output_format="gif", volume=0.5))
    assert "-af" not in cmd


def test_gif_width_is_forced_even():
    cmd = _cmd(VideoOps(output_format="gif", gif_width=481))
    assert "scale=480:-1" in cmd


# ---------------------------------------------------------------- containers

@pytest.mark.parametrize("ext,expected", [
    ("mp4", "mp4"), ("mov", "mp4"), ("m4v", "mp4"), ("mkv", "mkv"), ("webm", "webm"),
])
def test_container_family(ext, expected):
    assert container_family(ext) == expected


def test_unknown_extension_defaults_to_mp4():
    assert container_family("weird") == "mp4"
    assert container_family("") == "mp4"


def test_same_resolves_against_the_source():
    assert resolve_container("webm", "same") == "webm"
    assert resolve_container("mp4", "gif") == "gif"


def test_unknown_output_format_is_rejected():
    with pytest.raises(VideoOpsError):
        resolve_container("mp4", "avi")


# ------------------------------------------------------------------ frames

def test_frame_args_seek_then_take_exactly_one_frame():
    args = build_frame_args(
        ffmpeg="ffmpeg", source="in.mp4", destination="f.png", at_seconds=12.5
    )
    joined = " ".join(str(a) for a in args)
    assert joined.index("-ss 12.500") < joined.index("-i in.mp4")
    assert "-frames:v 1" in joined
    assert "-c:v png" in joined
    assert "-an" not in joined


def test_frame_args_scale_when_a_width_is_given():
    args = build_frame_args(
        ffmpeg="ffmpeg", source="in.mp4", destination="f.png",
        at_seconds=0.0, width=641,
    )
    joined = " ".join(str(a) for a in args)
    assert "scale=640:-2" in joined  # odd widths rounded down; -2 keeps aspect even


def test_frame_args_clamp_a_negative_timestamp():
    args = build_frame_args(
        ffmpeg="ffmpeg", source="in.mp4", destination="f.png", at_seconds=-5.0
    )
    assert "-ss 0.000" in " ".join(str(a) for a in args)


# ------------------------------------------------------------- safety

def test_rotation_must_be_a_quarter_turn():
    with pytest.raises(VideoOpsError):
        build_video_filters(VideoOps(rotate=45), container="mp4")


def test_streams_are_explicitly_mapped_and_extras_dropped():
    """Subtitles/attachments from a weird container must not leak through."""
    cmd = _cmd(VideoOps(trim_end=1.0))
    assert "-map 0:v:0" in cmd
    assert "-sn" in cmd
    assert "-dn" in cmd


def test_output_is_overwritten_without_an_interactive_prompt():
    cmd = _cmd(VideoOps(trim_end=1.0))
    assert " -y " in f" {cmd} "
    assert "-nostdin" in cmd


# -------------------------------------------------------------- properties

def test_op_flags_partition_the_work_correctly():
    silent_trim = VideoOps(trim_start=1.0)
    assert silent_trim.has_video_ops is False
    assert silent_trim.has_audio_ops is False

    volume_only = VideoOps(volume=0.5)
    assert volume_only.has_video_ops is False
    assert volume_only.has_audio_ops is True

    rotated = VideoOps(rotate=90)
    assert rotated.has_video_ops is True
    assert rotated.has_audio_ops is False

    # Muting is neither: the stream is dropped, not re-encoded.
    assert VideoOps(mute=True).has_audio_ops is False
