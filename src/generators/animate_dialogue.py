from __future__ import annotations

import hashlib
import math
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter
from src.models import Shot, VoiceDurationEntry


def _is_blinking(shot_id: str, frame_idx: int, fps: int = 12) -> bool:
    """Clignement des yeux déterministe (2 frames toutes les 3,5 à 5 secondes)."""
    seed = int(hashlib.md5(shot_id.encode("utf-8")).hexdigest()[:8], 16)
    interval = int((3.5 + (seed % 16) / 10.0) * fps)
    offset = seed % max(interval, 1)
    pos = (frame_idx + offset) % max(interval, 1)
    return pos in (0, 1)


def render_dialogue_shot(
    shot: Shot,
    bg_path: Path,
    sprite_pack: dict[str, Path],
    voice_entry: VoiceDurationEntry | None,
    out_clip: Path,
    resolution: tuple[int, int] = (1920, 1080),
    anim_fps: int = 12,
    delivery_fps: int = 24,
) -> Path:
    """Moteur Kuchipaku 0 € : fond flouté + léger zoom + sprite RGBA + lip-sync 3 états + clignement."""
    if out_clip.exists() and out_clip.stat().st_size > 0:
        return out_clip

    out_clip.parent.mkdir(parents=True, exist_ok=True)
    w, h = resolution
    total_frames = max(1, int(round(shot.duration_sec * anim_fps)))
    mouth_states = voice_entry.mouth_states_12fps if voice_entry else [0] * total_frames

    with Image.open(bg_path) as raw_bg:
        bg_base = raw_bg.convert("RGB").filter(ImageFilter.GaussianBlur(radius=2.2))

    sprites: dict[str, Image.Image] = {}
    target_char_h = int(h * 0.92)
    for key in (
        "m_closed_e_open", "m_half_e_open", "m_open_e_open",
        "m_closed_e_closed", "m_half_e_closed", "m_open_e_closed",
    ):
        with Image.open(sprite_pack[key]) as sp:
            rgba = sp.convert("RGBA")
            scale = target_char_h / max(rgba.height, 1)
            sprites[key] = rgba.resize((int(rgba.width * scale), target_char_h), Image.Resampling.LANCZOS)

    mouth_names = {0: "closed", 1: "half", 2: "open"}

    with tempfile.TemporaryDirectory(prefix=f"kuchipaku_{shot.shot_id}_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        for f_idx in range(total_frames):
            progress = f_idx / max(total_frames - 1, 1)
            zoom = 1.0 + 0.04 * progress
            crop_w = int(bg_base.width / zoom)
            crop_h = int(bg_base.height / zoom)
            left = (bg_base.width - crop_w) // 2
            top = (bg_base.height - crop_h) // 2
            frame = bg_base.crop((left, top, left + crop_w, top + crop_h)).resize(
                (w, h), Image.Resampling.BILINEAR
            )

            m_state = mouth_states[f_idx] if f_idx < len(mouth_states) else 0
            mouth_key = mouth_names.get(m_state, "closed")
            eye_key = "closed" if _is_blinking(shot.shot_id, f_idx, fps=anim_fps) else "open"
            sprite = sprites[f"m_{mouth_key}_e_{eye_key}"]

            breath_y = int(round(2.5 * math.sin(2.0 * math.pi * f_idx / (anim_fps * 2.8))))
            pos_x = (w - sprite.width) // 2
            pos_y = h - sprite.height + 12 + breath_y
            frame.paste(sprite, (pos_x, pos_y), sprite)
            frame.save(tmp_dir / f"frame_{f_idx:05d}.png")

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-framerate", str(anim_fps),
            "-i", str(tmp_dir / "frame_%05d.png"),
        ]
        if voice_entry and Path(voice_entry.wav_path).exists():
            cmd += [
                "-i", str(voice_entry.wav_path),
                "-t", f"{shot.duration_sec:.3f}",
                "-r", str(delivery_fps),
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-ac", "2",
                "-af", "apad",
                "-shortest",
                str(out_clip),
            ]
        else:
            cmd += [
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", f"{shot.duration_sec:.3f}",
                "-r", str(delivery_fps),
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-ac", "2",
                "-shortest",
                str(out_clip),
            ]
        subprocess.run(cmd, check=True)

    return out_clip
