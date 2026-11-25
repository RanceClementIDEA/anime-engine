from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

from src.models import EpisodeScript, SceneStoryboard


def compute_calibrated_post_fx(style_bible: dict[str, Any]) -> dict[str, float]:
    """Calibre automatiquement contraste, saturation, luminance, bloom et grain depuis style_bible.json."""
    metrics = style_bible.get("visual_metrics", {})
    post_fx = style_bible.get("post_fx", {})

    mean_luma = float(metrics.get("mean_luma", 0.28))
    mean_contrast = float(metrics.get("mean_contrast", 0.48))
    mean_sat = float(metrics.get("mean_saturation", 0.30))
    lighting_tag = str(metrics.get("lighting_tag", "dark_moody"))

    base_contrast = float(post_fx.get("contrast_boost", 1.06))
    base_sat = float(post_fx.get("saturation_scale", 0.94))
    base_bloom = float(post_fx.get("bloom_strength", 0.18))
    base_grain = float(post_fx.get("grain_strength", 8))

    # Ajustement doux depuis les métriques réelles de dataset/decors/
    contrast_delta = (mean_contrast - 0.45) * 0.18
    contrast = max(0.92, min(1.22, round(base_contrast + contrast_delta, 3)))

    sat_delta = (mean_sat - 0.35) * 0.20
    saturation = max(0.75, min(1.18, round(base_sat + sat_delta, 3)))

    if lighting_tag == "dark_moody" or mean_luma < 0.35:
        gamma = 0.94
        brightness = -0.015
    elif lighting_tag == "bright_daylight" or mean_luma > 0.62:
        gamma = 1.03
        brightness = 0.01
    else:
        gamma = 1.0
        brightness = 0.0

    bloom = max(0.0, min(0.45, round(base_bloom, 3)))
    grain = max(0, min(25, int(round(base_grain))))

    return {
        "contrast": contrast,
        "saturation": saturation,
        "gamma": gamma,
        "brightness": brightness,
        "bloom": bloom,
        "grain": float(grain),
    }


def build_post_fx_filtergraph(
    style_bible: dict[str, Any],
    input_label: str = "0:v",
    output_label: str = "vout",
) -> str:
    """Construit un filter_complex FFmpeg (bloom hautes lumières + eq + teinte unifiée + grain)."""
    fx = compute_calibrated_post_fx(style_bible)
    eq_filter = (
        f"eq=contrast={fx['contrast']:.3f}:saturation={fx['saturation']:.3f}"
        f":gamma={fx['gamma']:.3f}:brightness={fx['brightness']:.3f}"
    )

    metrics = style_bible.get("visual_metrics", {})
    lighting_tag = str(metrics.get("lighting_tag", "dark_moody"))
    if lighting_tag == "dark_moody":
        color_grade = "colorbalance=rs=-0.02:gs=-0.01:bs=0.03:rh=0.04:gh=0.01:bh=-0.03"
    else:
        color_grade = "colorbalance=rs=0.0:gs=0.0:bs=0.0:rh=0.02:gh=0.01:bh=-0.01"

    grain_val = int(fx["grain"])
    grain_filter = f",noise=alls={grain_val}:allf=t+u" if grain_val > 0 else ""

    if fx["bloom"] > 0.01:
        return (
            f"[{input_label}]format=yuv420p,split=2[base][glow_src];"
            f"[glow_src]eq=brightness=-0.12:contrast=1.35,gblur=sigma=14[glow];"
            f"[base][glow]blend=all_mode=screen:all_opacity={fx['bloom']:.3f},"
            f"{eq_filter},{color_grade}{grain_filter},format=yuv420p[{output_label}]"
        )

    return f"[{input_label}]format=yuv420p,{eq_filter},{color_grade}{grain_filter},format=yuv420p[{output_label}]"


def render_scene_master(
    scene_id: str,
    clip_paths: list[Path],
    out_scene_mp4: Path,
    style_bible: dict[str, Any],
    bgm_path: Path | None = None,
) -> Path:
    """Assemble les plans d'une scène, applique l'étalonnage/post-FX calibré et mixe la BGM."""
    if not clip_paths:
        raise ValueError(f"Aucun clip fourni pour la scène {scene_id}")

    out_scene_mp4.parent.mkdir(parents=True, exist_ok=True)
    fx = compute_calibrated_post_fx(style_bible)
    print(
        f"[compositing] {scene_id} -> contrast={fx['contrast']}, sat={fx['saturation']}, "
        f"gamma={fx['gamma']}, bloom={fx['bloom']}, grain={int(fx['grain'])}"
    )

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        concat_list = Path(tf.name)
        for cp in clip_paths:
            safe_path = cp.resolve().as_posix().replace("'", "'\\''")
            tf.write(f"file '{safe_path}'\n")

    try:
        v_graph = build_post_fx_filtergraph(style_bible, input_label="0:v", output_label="vout")
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
        ]

        if bgm_path and bgm_path.exists():
            cmd.extend(["-stream_loop", "-1", "-i", str(bgm_path)])
            a_graph = "[1:a]volume=0.16[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            filter_complex = f"{v_graph};{a_graph}"
            cmd.extend([
                "-filter_complex", filter_complex,
                "-map", "[vout]", "-map", "[aout]",
            ])
        else:
            cmd.extend([
                "-filter_complex", v_graph,
                "-map", "[vout]", "-map", "0:a",
            ])

        cmd.extend([
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(out_scene_mp4),
        ])
        subprocess.run(cmd, check=True)
    finally:
        concat_list.unlink(missing_ok=True)

    return out_scene_mp4


def assemble_episode_master(rendered_scenes: list[Path], out_master_mp4: Path) -> Path:
    """Concatène les scènes rendues en un master épisode propre."""
    if not rendered_scenes:
        raise ValueError("Aucune scène rendue à assembler")

    out_master_mp4.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        concat_list = Path(tf.name)
        for sp in rendered_scenes:
            safe_path = sp.resolve().as_posix().replace("'", "'\\''")
            tf.write(f"file '{safe_path}'\n")

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-c", "copy",
                str(out_master_mp4),
            ],
            check=True,
        )
    finally:
        concat_list.unlink(missing_ok=True)

    return out_master_mp4


def _format_ass_time(sec: float) -> str:
    total_cs = max(0, int(round(sec * 100)))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def generate_ass_subtitles(
    episode_script: EpisodeScript,
    scenes: list[SceneStoryboard],
    out_ass_path: Path,
) -> Path:
    """Génère les sous-titres français .ass synchronisés sur les durées des plans."""
    out_ass_path.parent.mkdir(parents=True, exist_ok=True)

    lines_by_id: dict[str, str] = {}
    for sc in episode_script.scenes:
        for dl in sc.dialogue_lines:
            lines_by_id[dl.line_id] = dl.text.strip()

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,54,&H00FFFFFF,&H000000FF,&H00101018,&H80000000,-1,0,0,0,100,100,0,0,1,3,1.5,2,90,90,56,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events: list[str] = []
    cursor = 0.0
    for scene in scenes:
        for shot in scene.shots:
            start = cursor
            end = cursor + shot.duration_sec
            if shot.line_id and shot.line_id in lines_by_id:
                txt = lines_by_id[shot.line_id].replace("\n", "\\N")
                if txt:
                    events.append(
                        f"Dialogue: 0,{_format_ass_time(start + 0.08)},{_format_ass_time(max(start + 0.3, end - 0.08))},Default,,0,0,0,,{txt}"
                    )
            cursor = end

    out_ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_ass_path


def burn_subtitles_vostfr(
    master_mp4: Path,
    ass_path: Path,
    output_vostfr_mp4: Path,
) -> Path:
    """Crée la version VOSTFR avec sous-titres .ass incrustés (chemin relatif compatible Windows)."""
    output_vostfr_mp4.parent.mkdir(parents=True, exist_ok=True)
    rel_ass = ass_path.as_posix().replace(":", "\\:")
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(master_mp4),
            "-vf", f"ass={rel_ass}",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            str(output_vostfr_mp4),
        ],
        check=True,
    )
    return output_vostfr_mp4