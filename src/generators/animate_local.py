from __future__ import annotations

import subprocess
from pathlib import Path
from src.models import Shot


def _run_ffmpeg(args: list[str]) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    subprocess.run(cmd, check=True)


def render_local_shot(
    shot: Shot,
    start_frame: Path,
    out_clip: Path,
    foreground_rgba: Path | None = None,
    resolution: tuple[int, int] = (1920, 1080),
    fps: int = 24,
) -> Path:
    """Rendu 0 € pour static_pan, insert_cut, impact_frame et parallax_2_5d."""
    if out_clip.exists() and out_clip.stat().st_size > 0:
        return out_clip

    out_clip.parent.mkdir(parents=True, exist_ok=True)
    w, h = resolution
    total_frames = max(1, int(round(shot.duration_sec * fps)))

    if shot.render_mode == "impact_frame":
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},format=gray,negate,lut=y='if(gt(val,128),255,0)'"
        )
        _run_ffmpeg([
            "-loop", "1", "-i", str(start_frame),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", f"{shot.duration_sec:.3f}",
            "-vf", vf, "-r", str(fps),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-shortest", str(out_clip),
        ])
        return out_clip

    if shot.render_mode == "parallax_2_5d" and foreground_rgba and foreground_rgba.exists():
        filter_complex = (
            f"[0:v]scale={w + 220}:{h + 120}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h}:x='min(200, (t/{shot.duration_sec:.3f})*90)':y='60'[bg];"
            f"[1:v]scale=-1:{int(h * 0.90)}[fg];"
            f"[bg][fg]overlay=x='(W-w)/2 - 80 + (t/{shot.duration_sec:.3f})*190':y='H-h+10'[vout]"
        )
        _run_ffmpeg([
            "-loop", "1", "-i", str(start_frame),
            "-loop", "1", "-i", str(foreground_rgba),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", f"{shot.duration_sec:.3f}",
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "2:a",
            "-r", str(fps),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-shortest", str(out_clip),
        ])
        return out_clip

    scale_up = f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2}"
    if shot.camera_move == "zoom_in":
        zp = f"zoompan=z='min(zoom+0.0018,1.22)':d={total_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}"
    elif shot.camera_move == "zoom_out":
        zp = f"zoompan=z='max(1.22-0.0018*on,1.0)':d={total_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}"
    elif shot.camera_move == "pan_left":
        zp = f"zoompan=z=1.15:d={total_frames}:x='(iw-iw/zoom)*(1-on/{total_frames})':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}"
    elif shot.camera_move == "pan_right":
        zp = f"zoompan=z=1.15:d={total_frames}:x='(iw-iw/zoom)*(on/{total_frames})':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}"
    elif shot.camera_move == "shake":
        zp = (
            f"zoompan=z=1.12:d={total_frames}:"
            f"x='iw/2-(iw/zoom/2)+18*sin(on*1.9)':"
            f"y='ih/2-(ih/zoom/2)+14*cos(on*2.3)':s={w}x{h}:fps={fps}"
        )
    else:
        zp = f"zoompan=z=1.0:d={total_frames}:x='0':y='0':s={w}x{h}:fps={fps}"

    _run_ffmpeg([
        "-loop", "1", "-i", str(start_frame),
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", f"{shot.duration_sec:.3f}",
        "-vf", f"{scale_up},{zp}",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-shortest", str(out_clip),
    ])
    return out_clip
