from __future__ import annotations

import argparse
import colorsys
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
NOISE_TAGS = {
    "highres", "absurdres", "animated", "video", "presumed",
    "production_materials", "character_sheet", "sakuga", "lowres",
}


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def analyze_image_visuals(path: Path) -> dict[str, Any] | None:
    try:
        with Image.open(path) as img:
            width, height = img.size
            rgb = img.convert("RGB")
            thumb = rgb.copy()
            thumb.thumbnail((160, 160))

            quantized = thumb.quantize(colors=5, method=Image.Quantize.MEDIANCUT)
            palette = quantized.getpalette() or []
            color_counts = sorted(
                quantized.getcolors() or [], key=lambda x: x[0], reverse=True
            )

            top_colors: list[str] = []
            sats: list[float] = []
            for _, idx in color_counts[:5]:
                r, g, b = palette[idx * 3 : idx * 3 + 3]
                top_colors.append(rgb_to_hex((r, g, b)))
                _, s, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                sats.append(s)

            gray_stat = ImageStat.Stat(thumb.convert("L"))
            return {
                "width": width,
                "height": height,
                "aspect_ratio": round(width / max(height, 1), 2),
                "mean_luma": round(gray_stat.mean[0] / 255.0, 3),
                "contrast_std": round(gray_stat.stddev[0] / 255.0, 3),
                "mean_saturation": round(sum(sats) / max(len(sats), 1), 3),
                "top_colors": top_colors,
            }
    except Exception:
        return None


def load_records(dataset_dir: Path, metadata_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if metadata_path.exists():
        with metadata_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                local_path = Path(item.get("local_path", ""))
                if not local_path.is_absolute():
                    local_path = dataset_dir / local_path
                if local_path.exists():
                    item["_resolved_path"] = local_path
                    records.append(item)
        if records:
            return records

    for path in dataset_dir.rglob("*"):
        if path.suffix.lower() in IMAGE_EXTS and path.is_file():
            rel_parts = path.relative_to(dataset_dir).parts
            category = rel_parts[0] if len(rel_parts) > 1 else "uncategorized"
            records.append(
                {
                    "id": path.stem,
                    "category": category,
                    "tags": [],
                    "artist": "unknown",
                    "anime_title": "unknown",
                    "blur_score": 0.0,
                    "_resolved_path": path,
                }
            )
    return records


def infer_lighting_profile(luma: float, contrast: float, saturation: float) -> str:
    tone = "dark_moody" if luma < 0.38 else ("bright_daylight" if luma > 0.62 else "balanced_midtone")
    shading = "high_contrast_hard_cel" if contrast > 0.24 else "soft_atmospheric_cel"
    chroma = "vivid_saturated" if saturation > 0.45 else "muted_cinematic"
    return f"{tone}, {shading}, {chroma}"


def build_style_bible(
    dataset_dir: Path,
    metadata_path: Path,
    output_path: Path,
    style_name: str,
    sample_limit: int = 200,
    refs_per_category: int = 3,
) -> dict[str, Any]:
    records = load_records(dataset_dir, metadata_path)
    if not records:
        raise FileNotFoundError(f"Aucune image trouvée dans {dataset_dir}")

    global_tags: Counter[str] = Counter()
    tags_by_cat: dict[str, Counter[str]] = defaultdict(Counter)
    artists: Counter[str] = Counter()
    series: Counter[str] = Counter()
    color_counter: Counter[str] = Counter()
    ratios: Counter[float] = Counter()
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)

    lumas: list[float] = []
    contrasts: list[float] = []
    saturations: list[float] = []

    for idx, rec in enumerate(records):
        cat = str(rec.get("category", "uncategorized")).lower()
        by_category[cat].append(rec)

        for tag in rec.get("tags", []):
            clean = str(tag).strip().lower()
            if clean and clean not in NOISE_TAGS:
                global_tags[clean] += 1
                tags_by_cat[cat][clean] += 1

        artist = rec.get("artist")
        if artist and artist != "unknown":
            artists[str(artist)] += 1

        anime = rec.get("anime_title")
        if anime and anime != "unknown":
            series[str(anime)] += 1

        if idx < sample_limit:
            visual = analyze_image_visuals(rec["_resolved_path"])
            if visual:
                rec["_visual"] = visual
                ratios[visual["aspect_ratio"]] += 1
                lumas.append(visual["mean_luma"])
                contrasts.append(visual["contrast_std"])
                saturations.append(visual["mean_saturation"])
                color_counter.update(visual["top_colors"])

    reference_pack: dict[str, list[dict[str, Any]]] = {}
    for cat, items in by_category.items():
        ranked = sorted(
            items,
            key=lambda r: (
                float(r.get("blur_score") or 0.0),
                int(r.get("width") or r.get("_visual", {}).get("width", 0)),
            ),
            reverse=True,
        )
        reference_pack[cat] = [
            {
                "id": str(r.get("id")),
                "path": str(r["_resolved_path"]),
                "artist": r.get("artist", "unknown"),
                "anime_title": r.get("anime_title", "unknown"),
                "top_tags": list(r.get("tags", []))[:8],
            }
            for r in ranked[:refs_per_category]
        ]

    avg_luma = round(sum(lumas) / max(len(lumas), 1), 3)
    avg_contrast = round(sum(contrasts) / max(len(contrasts), 1), 3)
    avg_sat = round(sum(saturations) / max(len(saturations), 1), 3)
    lighting_profile = infer_lighting_profile(avg_luma, avg_contrast, avg_sat)
    top_global_tags = [t for t, _ in global_tags.most_common(15)]

    style_bible: dict[str, Any] = {
        "meta": {
            "style_name": style_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_images_indexed": len(records),
            "categories_count": {k: len(v) for k, v in by_category.items()},
        },
        "visual_dna": {
            "dominant_aspect_ratio": ratios.most_common(1)[0][0] if ratios else 1.78,
            "dominant_palette_hex": [c for c, _ in color_counter.most_common(10)],
            "metrics": {
                "mean_luma": avg_luma,
                "mean_contrast": avg_contrast,
                "mean_saturation": avg_sat,
            },
            "lighting_profile": lighting_profile,
            "top_artists": [a for a, _ in artists.most_common(10)],
            "top_series": [s for s, _ in series.most_common(10)],
        },
        "tag_vocabulary": {
            "global_top_tags": top_global_tags,
            "by_category": {
                cat: [t for t, _ in counter.most_common(12)]
                for cat, counter in tags_by_cat.items()
            },
        },
        "prompt_templates": {
            "positive_prefix": (
                f"japanese anime style, {style_name}, {lighting_profile}, "
                "clean lineart, flat cel shading"
                + (f", {', '.join(top_global_tags[:6])}" if top_global_tags else "")
            ),
            "negative_prompt": (
                "3d render, photorealistic, plastic skin, blurry, watermark, "
                "text, speech bubble, bad anatomy, deformed hands, extra limbs"
            ),
        },
        "animation_rules": {
            "target_fps": 12,
            "delivery_fps": 24,
            "budget_split": {"local_free_ratio": 0.85, "i2v_hero_ratio": 0.15},
            "post_fx": {
                "bloom_opacity": 0.22,
                "film_grain_strength": 6,
                "use_impact_frames_on_hits": True,
            },
        },
        "claude_vision_reference_pack": reference_pack,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(style_bible, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return style_bible


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyse un dataset anime vers style_bible.json")
    parser.add_argument("--dataset", type=Path, default=Path("dataset"))
    parser.add_argument("--metadata", type=Path, default=Path("dataset/metadata.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("config/style_bible.json"))
    parser.add_argument("--style-name", type=str, default="dark horlo-punk cel-shaded anime")
    parser.add_argument("--sample-limit", type=int, default=200)
    args = parser.parse_args()

    bible = build_style_bible(
        dataset_dir=args.dataset,
        metadata_path=args.metadata,
        output_path=args.out,
        style_name=args.style_name,
        sample_limit=args.sample_limit,
    )
    print(f"OK -> {args.out} ({bible['meta']['total_images_indexed']} images indexées)")


if __name__ == "__main__":
    main()
