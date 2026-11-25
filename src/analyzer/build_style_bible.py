from __future__ import annotations

import argparse
import json
import math
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat

CATEGORIES = ("decors", "settei", "props", "genga", "acting", "fx", "smears", "cycles")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".gif"}


def ensure_dataset_folders(dataset_dir: Path) -> None:
    """Crée les 8 sous-dossiers standards dans dataset/ s'ils n'existent pas."""
    for cat in CATEGORIES:
        d = dataset_dir / cat
        d.mkdir(parents=True, exist_ok=True)
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists() and not any(d.iterdir()):
            gitkeep.write_text("", encoding="utf-8")


def _probe_duration_sec(video_path: Path) -> float:
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        val = float(res.stdout.strip())
        return val if math.isfinite(val) and val > 0 else 2.0
    except Exception:
        return 2.0


def extract_video_keyframes(
    video_path: Path,
    out_dir: Path,
    num_frames: int = 3,
) -> list[Path]:
    """Extrait num_frames keyframes d'un clip Sakugabooru (.mp4/.webm/.gif) de manière idempotente."""
    out_dir.mkdir(parents=True, exist_ok=True)
    expected = [out_dir / f"{video_path.stem}_kf{i + 1}.png" for i in range(num_frames)]
    if all(p.exists() and p.stat().st_size > 0 for p in expected):
        return expected

    duration = _probe_duration_sec(video_path)
    ratios = [0.2, 0.5, 0.8] if num_frames == 3 else [(i + 1) / (num_frames + 1) for i in range(num_frames)]
    extracted: list[Path] = []

    for idx, ratio in enumerate(ratios):
        target = expected[idx]
        if target.exists() and target.stat().st_size > 0:
            extracted.append(target)
            continue
        ts = max(0.0, duration * ratio)
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{ts:.3f}",
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-q:v", "2",
                    str(target),
                ],
                check=True,
            )
            if target.exists() and target.stat().st_size > 0:
                extracted.append(target)
        except Exception:
            continue

    return extracted


def _analyze_image_metrics(img_path: Path) -> tuple[float, float, float, list[tuple[int, int, int]]]:
    """Retourne (luma_0_1, contrast_0_1, saturation_0_1, couleurs_dominantes_rgb)."""
    with Image.open(img_path) as im:
        rgb = im.convert("RGB")
        thumb = rgb.copy()
        thumb.thumbnail((320, 320))

        gray = thumb.convert("L")
        stat_l = ImageStat.Stat(gray)
        luma = stat_l.mean[0] / 255.0
        contrast = stat_l.stddev[0] / 128.0

        hsv = thumb.convert("HSV")
        stat_s = ImageStat.Stat(hsv.split()[1])
        saturation = stat_s.mean[0] / 255.0

        quant = thumb.quantize(colors=8, method=Image.Quantize.MEDIANCUT).convert("RGB")
        pixels = list(quant.getdata())
        top_colors = [c for c, _ in Counter(pixels).most_common(6)]

    return luma, contrast, saturation, top_colors


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def _classify_style_tags(luma: float, contrast: float, saturation: float) -> tuple[str, str, str]:
    lighting = "dark_moody" if luma < 0.38 else ("balanced_cinematic" if luma < 0.62 else "bright_daylight")
    shading = "high_contrast_hard_cel" if contrast > 0.38 else "soft_cel_shading"
    color_mood = "muted_cinematic" if saturation < 0.35 else ("rich_cinematic" if saturation < 0.60 else "vivid_anime")
    return lighting, shading, color_mood


def build_style_bible(
    dataset_dir: Path = Path("dataset"),
    output_path: Path = Path("config/style_bible.json"),
    keyframes_per_video: int = 3,
) -> dict[str, Any]:
    ensure_dataset_folders(dataset_dir)
    cache_dir = dataset_dir / "_extracted_keyframes"

    category_counts: dict[str, int] = {}
    category_videos: dict[str, int] = {}
    reference_pack: dict[str, list[str]] = {}
    decor_images: list[Path] = []
    total_images = 0
    total_videos = 0

    for cat in CATEGORIES:
        cat_dir = dataset_dir / cat
        images: list[Path] = []
        videos: list[Path] = []

        for p in sorted(cat_dir.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            ext = p.suffix.lower()
            if ext in IMAGE_EXTS:
                images.append(p)
            elif ext in VIDEO_EXTS:
                videos.append(p)

        extracted_kfs: list[Path] = []
        for v in videos:
            extracted_kfs.extend(extract_video_keyframes(v, cache_dir / cat, num_frames=keyframes_per_video))

        all_cat_images = images + extracted_kfs
        category_counts[cat] = len(all_cat_images)
        category_videos[cat] = len(videos)
        total_images += len(all_cat_images)
        total_videos += len(videos)

        if all_cat_images:
            step = max(1, len(all_cat_images) // 3)
            picks = all_cat_images[::step][:3]
            reference_pack[cat] = [p.as_posix() for p in picks]
        else:
            reference_pack[cat] = []

        if cat == "decors":
            decor_images = all_cat_images

    # Calcul des métriques visuelles ISOLÉ sur dataset/decors/
    lumas: list[float] = []
    contrasts: list[float] = []
    saturations: list[float] = []
    color_counter: Counter[tuple[int, int, int]] = Counter()

    for img_path in decor_images:
        try:
            luma, contrast, sat, top_rgb = _analyze_image_metrics(img_path)
            lumas.append(luma)
            contrasts.append(contrast)
            saturations.append(sat)
            for idx, col in enumerate(top_rgb):
                bucket = (col[0] // 16 * 16, col[1] // 16 * 16, col[2] // 16 * 16)
                color_counter[bucket] += max(1, 6 - idx)
        except Exception:
            continue

    if lumas:
        mean_luma = round(sum(lumas) / len(lumas), 3)
        mean_contrast = round(sum(contrasts) / len(contrasts), 3)
        mean_sat = round(sum(saturations) / len(saturations), 3)
        palette_hex = [_rgb_to_hex(c) for c, _ in color_counter.most_common(6)]
        metrics_source = f"decors ({len(lumas)} images)"
    else:
        mean_luma = 0.28
        mean_contrast = 0.48
        mean_sat = 0.30
        palette_hex = ["#0F1424", "#1E293B", "#475569", "#D97706", "#F59E0B", "#E2E8F0"]
        metrics_source = "default_fallback (aucun decor indexe)"

    lighting_tag, shading_tag, color_tag = _classify_style_tags(mean_luma, mean_contrast, mean_sat)

    existing: dict[str, Any] = {}
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    style_bible: dict[str, Any] = {
        "project": existing.get("project", "Meridien Zero"),
        "dataset_summary": {
            "total_images_indexed": total_images,
            "total_videos_extracted": total_videos,
            "metrics_source": metrics_source,
            "by_category": category_counts,
            "videos_by_category": category_videos,
        },
        "visual_metrics": {
            "mean_luma": mean_luma,
            "mean_contrast": mean_contrast,
            "mean_saturation": mean_sat,
            "lighting_tag": lighting_tag,
            "shading_tag": shading_tag,
            "color_tag": color_tag,
            "dominant_palette_hex": palette_hex,
        },
        "reference_pack": reference_pack,
        "prompt_templates": {
            "positive_prefix": (
                f"japanese anime style, dark horlo-punk cel-shaded anime, "
                f"{lighting_tag}, {shading_tag}, {color_tag}, "
                f"clean lineart, flat cel shading, dramatic amber rim lighting"
            ),
            "background_suffix": (
                "makoto shinkai and ufotable inspired anime background art, painterly architecture, "
                "deep atmospheric perspective, volumetric dust motes, no characters"
            ),
            "character_lineart": (
                "anime production settei quality, crisp variable-weight ink lineart, "
                "2-tone hard cel shadows, expressive anime eyes, consistent proportions"
            ),
            "acting_closeup": (
                "sakuga character acting keyframe, subtle emotional micro-expression, "
                "cinematic shallow depth of field, dramatic key light on face"
            ),
            "action_sakuga": (
                "high-impact japanese sakuga animation, dynamic camera angle, "
                "subtle smear frames, glowing amber energy particles, crisp impact sparks, animated on twos"
            ),
            "negative_prompt": (
                "3d render, cgi, photorealistic, western cartoon, blurry, muddy colors, "
                "extra fingers, deformed anatomy, watermark, text, logo"
            ),
        },
        "post_fx": existing.get(
            "post_fx",
            {
                "bloom_strength": 0.18,
                "grain_strength": 8,
                "contrast_boost": 1.06,
                "saturation_scale": 0.94,
            },
        ),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(style_bible, indent=2, ensure_ascii=False), encoding="utf-8")
    return style_bible


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyse dataset/ (8 catégories + vidéos Sakugabooru) -> config/style_bible.json")
    parser.add_argument("--dataset", type=Path, default=Path("dataset"))
    parser.add_argument("--output", type=Path, default=Path("config/style_bible.json"))
    parser.add_argument("--keyframes", type=int, default=3, help="Nombre de keyframes extraites par vidéo")
    args = parser.parse_args()

    bible = build_style_bible(args.dataset, args.output, keyframes_per_video=args.keyframes)
    summary = bible["dataset_summary"]
    metrics = bible["visual_metrics"]
    print(f"OK -> {args.output} mis a jour")
    print(f"   Images indexees : {summary['total_images_indexed']} (dont {summary['total_videos_extracted']} videos extraites)")
    print(f"   Source palette  : {summary['metrics_source']}")
    print(f"   Palette         : {', '.join(metrics['dominant_palette_hex'])}")
    print(f"   Categories      : {summary['by_category']}")


if __name__ == "__main__":
    main()