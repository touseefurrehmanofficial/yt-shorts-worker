"""Edits a downloaded reel: shrinks it slightly to make room for a black
title bar on top (no cropping, full aspect ratio preserved), writes the
video name in that bar, and overlays a single watermark that continuously
drifts around the frame (DVD-bounce style) so it can't be trivially cropped
out or reused elsewhere.

Audio: the original reel audio is muted entirely (source reels are
copyrighted; re-uploading their audio risks Content ID claims) and replaced
with a freshly synthesized, original background music track (see audio_gen.py)
-- randomized per video, generated from code, so there is nothing to claim.
"""
import json
import random
import subprocess
import sys
from pathlib import Path

import audio_gen

ROOT = Path(__file__).resolve().parent.parent
FONT = "C:/Windows/Fonts/arialbd.ttf".replace(":", "\\:")
MASCOT_DIR = ROOT / "assets"
MASCOT_GLOB = "cartoon_mascot_*.png"
MUSIC_VOLUME = 1.0            # tracks are loudness-normalized to -22 dB mean
MAX_EDGE = 720                # cap the longest video edge at 720p
MUSIC_FADE_SECONDS = 1.2


def pick_random_mascot() -> Path:
    options = sorted(MASCOT_DIR.glob(MASCOT_GLOB))
    if not options:
        raise FileNotFoundError(f"No mascot images found in {MASCOT_DIR} matching {MASCOT_GLOB}")
    return random.choice(options)

WATERMARK_TEXT = "TalkieVerse 3D"
BAR_RATIO = 0.12       # fraction of height reserved for the top title bar
WATERMARK_MARGIN = 18
WM_SPEED_X = 0.13
WM_SPEED_Y = 0.19
MASCOT_WIDTH_RATIO = 0.32   # mascot width as a fraction of video width
MASCOT_MARGIN_RATIO = 0.03  # gap from the bottom-right edges


def _esc(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def probe_dims(path: Path):
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(out.stdout)["streams"][0]
    return int(info["width"]), int(info["height"])


def build_filter(width: int, height: int, video_name: str) -> str:
    bar_h = int(round(height * BAR_RATIO / 2) * 2)          # keep even
    video_h = height - bar_h
    if video_h % 2:
        video_h -= 1
        bar_h += 1

    title_fs = max(18, bar_h // 2)
    wm_fs = max(16, height // 28)

    name_esc = _esc(video_name)
    wm_esc = _esc(WATERMARK_TEXT)

    wm_y_min = bar_h + WATERMARK_MARGIN
    wm_y_span = height - bar_h - 2 * WATERMARK_MARGIN

    filters = [
        f"scale={width}:{video_h}:force_original_aspect_ratio=decrease",
        f"pad={width}:{video_h}:(ow-iw)/2:(oh-ih)/2:black",
        f"pad={width}:{height}:0:{bar_h}:black",
        (
            f"drawtext=fontfile='{FONT}':text='{name_esc}':fontcolor=white:"
            f"fontsize={title_fs}:x=(w-text_w)/2:y=({bar_h}-text_h)/2"
        ),
        (
            f"drawtext=fontfile='{FONT}':text='{wm_esc}':fontcolor=white@0.85:"
            f"fontsize={wm_fs}:borderw=2:bordercolor=black@0.55:"
            f"x='(w-text_w)*abs(mod(t*{WM_SPEED_X}\\,2)-1)':"
            f"y='{wm_y_min}+({wm_y_span})*abs(mod(t*{WM_SPEED_Y}\\,2)-1)'"
        ),
    ]
    return ",".join(filters)


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def edit_video(input_path: Path, output_path: Path, video_name: str, trim_start: float = 0.0):
    width, height = probe_dims(input_path)
    # Cap the output at 720p on the longest edge (faster encode + upload,
    # still HD -- plenty for phone screens and Shorts).
    if max(width, height) > MAX_EDGE:
        k = MAX_EDGE / max(width, height)
        width = int(round(width * k / 2) * 2)
        height = int(round(height * k / 2) * 2)
    base_chain = build_filter(width, height, video_name)

    mascot_path = pick_random_mascot()
    mascot_w = int(round(width * MASCOT_WIDTH_RATIO / 2) * 2)
    margin = int(round(width * MASCOT_MARGIN_RATIO))

    # Mute the source audio entirely and synthesize an original, random
    # genre background track instead (no copyrighted material at all).
    import genre_tracks
    genre = random.choice(list(genre_tracks.GENRES))
    out_duration = max(1.0, probe_duration(input_path) - trim_start)
    music_path = output_path.with_suffix(".music.wav")
    samples = genre_tracks.render_genre(genre, out_duration, seed=random.randint(0, 2**31))
    audio_gen.write_wav(samples, music_path)
    fade_out_at = max(0.0, out_duration - MUSIC_FADE_SECONDS)

    filter_complex = (
        f"[0:v]{base_chain}[base];"
        f"[1:v]scale={mascot_w}:-1[mascot];"
        f"[base][mascot]overlay=x=W-w-{margin}:y=H-h-{margin}[vout];"
        f"[2:a]volume={MUSIC_VOLUME},"
        f"afade=t=in:st=0:d={MUSIC_FADE_SECONDS},"
        f"afade=t=out:st={fade_out_at:.2f}:d={MUSIC_FADE_SECONDS}[aout]"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y"]
    if trim_start > 0:
        cmd += ["-ss", f"{trim_start:.2f}"]
    cmd += [
        "-i", str(input_path),
        "-loop", "1", "-i", str(mascot_path),
        "-i", str(music_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed for {input_path}:\n{result.stderr[-4000:]}")
    finally:
        try:
            music_path.unlink(missing_ok=True)
        except OSError:
            pass
    return output_path


if __name__ == "__main__":
    # quick self-test on a synthetic vertical test clip
    test_dir = ROOT / "logs"
    test_dir.mkdir(exist_ok=True)
    synth = test_dir / "synth_input.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "testsrc=size=720x1280:duration=6:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(synth),
        ],
        capture_output=True, text=True, check=True,
    )
    out = edit_video(synth, test_dir / "synth_edited.mp4", "Pure Talent 1")
    print("Self-test output:", out)
