from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from src.models import EpisodeScript, SceneStoryboard


def _format_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def generate_ass_subtitles(
    episode: EpisodeScript,
    scenes: list[SceneStoryboard],
    out_ass: Path,
) -> Path:
    """Génère des sous-titres .ass synchronisés sur les durées des plans."""
    text_by_line: dict[str, tuple[str, str]] = {}
    for sc in episode.scenes:
        for line in sc.dialogue_lines:
            text_by_line[line.line_id] = (line.character_id or "", line.text)

    header = (
        "[Script Info]\n"
        f"Title: {episode.episode_title}\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: AnimeDefault,Arial,54,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,80,80,55,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events: list[str] = []
    cursor = 0.0
    for sc in scenes:
        for shot in sc.shots:
            start_t = cursor
            end_t = cursor + shot.duration_sec
            if shot.line_id and shot.line_id in text_by_line:
                speaker, text = text_by_line[shot.line_id]
                safe_text = text.replace("\n", "\\N")
                events.append(
                    f"Dialogue: 0,{_format_ass_time(start_t)},{_format_ass_time(end_t)},"
                    f"AnimeDefault,{speaker},0,0,0,,{safe_text}"
                )
            cursor = end_t

    out_ass.parent.mkdir(parents=True, exist_ok=True)
    out_ass.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_ass


def render_scene_master(
    scene_id: str,
    clip_paths: list[Path],
    out_scene_mp4: Path,
    style_bible: dict[str, Any],
    bgm_path: Path | None = None,
) -> Path:
    """Étage 1 (Anti-OOM) : assemble une scène + 12 fps on twos + bloom + grain + auto-ducking BGM."""
    out_scene_mp4.parent.mkdir(parents=True, exist_ok=True)
    concat_list = out_scene_mp4.parent / f"_{scene_id}_concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in clip_paths),
        encoding="utf-8",
    )

    fx = style_bible.get("animation_rules", {}).get("post_fx", {})
    bloom = float(fx.get("bloom_opacity", 0.22))
    grain = int(fx.get("film_grain_strength", 6))

    video_chain = (
        f"[0:v]fps=12,fps=24,split=2[base][glow];"
        f"[glow]eq=brightness=-0.12:contrast=1.45,gblur=sigma=14[blur];"
        f"[base][blur]blend=all_mode=screen:all_opacity={bloom},"
        f"noise=alls={grain}:allf=t+u[vout]"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
    ]

    if bgm_path and bgm_path.exists():
        audio_chain = (
            "[0:a]highpass=f=80,asplit=2[voice][sc];"
            "[1:a]volume=0.35[bgm];"
            "[bgm][sc]sidechaincompress=threshold=0.04:ratio=6:attack=25:release=300[bgm_duck];"
            "[voice][bgm_duck]amix=inputs=2:duration=first[aout]"
        )
        cmd += [
            "-stream_loop", "-1", "-i", str(bgm_path),
            "-filter_complex", f"{video_chain};{audio_chain}",
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            str(out_scene_mp4),
        ]
    else:
        cmd += [
            "-filter_complex", video_chain,
            "-map", "[vout]", "-map", "0:a",
            "-af", "highpass=f=80",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            str(out_scene_mp4),
        ]

    subprocess.run(cmd, check=True)
    concat_list.unlink(missing_ok=True)
    return out_scene_mp4


def assemble_episode_master(
    scene_mp4s: list[Path],
    output_mp4: Path,
    bank_dir: Path = Path("bank"),
) -> Path:
    """Étage 2 : concatène les scènes rendues (+ OP/ED si présents) et normalise à -14 LUFS."""
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    ordered_clips: list[Path] = []

    op_file = bank_dir / "opening" / "op_master.mp4"
    ed_file = bank_dir / "ending" / "ed_master.mp4"

    if scene_mp4s:
        ordered_clips.append(scene_mp4s[0])
        if op_file.exists():
            ordered_clips.append(op_file)
        ordered_clips.extend(scene_mp4s[1:])
        if ed_file.exists():
            ordered_clips.append(ed_file)

    concat_list = output_mp4.parent / "_episode_concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in ordered_clips),
        encoding="utf-8",
    )

    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c:v", "copy",
            "-af", "loudnorm=I=-14:TP=-1.0:LRA=11",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            str(output_mp4),
        ],
        check=True,
    )
    concat_list.unlink(missing_ok=True)
    return output_mp4
