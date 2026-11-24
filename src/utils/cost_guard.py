from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from src.models import CostLogEntry, EpisodeScript, SceneStoryboard

ESTIMATED_IMG_USD = 0.02
ESTIMATED_I2V_USD_PER_SEC = 0.05  # Toujours avec generate_audio=False


class BudgetExceededError(RuntimeError):
    """Levée lorsque le double coupe-circuit budgétaire ou le ratio I2V est dépassé."""


@dataclass(frozen=True)
class CostReport:
    total_duration_sec: float
    i2v_duration_sec: float
    i2v_ratio: float
    unique_images_count: int
    images_usd: float
    i2v_usd: float
    total_usd: float
    bank_reuse_ratio: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "total_duration_sec": round(self.total_duration_sec, 2),
            "i2v_duration_sec": round(self.i2v_duration_sec, 2),
            "i2v_ratio_pct": round(self.i2v_ratio * 100.0, 1),
            "unique_images_count": self.unique_images_count,
            "images_usd": round(self.images_usd, 2),
            "i2v_usd": round(self.i2v_usd, 2),
            "total_usd": round(self.total_usd, 2),
            "bank_reuse_ratio_pct": round(self.bank_reuse_ratio * 100.0, 1),
        }


def estimate_scene_cost(scene: SceneStoryboard) -> CostReport:
    total_sec = sum(s.duration_sec for s in scene.shots)
    i2v_sec = sum(s.duration_sec for s in scene.shots if s.render_mode == "action_i2v")
    unique_images = sum(
        1
        for s in scene.shots
        if not (s.render_mode == "dialogue_lipsync" and s.reuse_master_bg)
        and not (s.render_mode == "static_pan" and s.reuse_master_bg and s.location_id)
    )
    reused_shots = sum(1 for s in scene.shots if s.reuse_master_bg)
    img_usd = unique_images * ESTIMATED_IMG_USD
    i2v_usd = i2v_sec * ESTIMATED_I2V_USD_PER_SEC
    return CostReport(
        total_duration_sec=total_sec,
        i2v_duration_sec=i2v_sec,
        i2v_ratio=i2v_sec / max(total_sec, 0.001),
        unique_images_count=unique_images,
        images_usd=img_usd,
        i2v_usd=i2v_usd,
        total_usd=img_usd + i2v_usd,
        bank_reuse_ratio=reused_shots / max(len(scene.shots), 1),
    )


def estimate_episode_cost(scenes: list[SceneStoryboard]) -> CostReport:
    reports = [estimate_scene_cost(sc) for sc in scenes]
    total_sec = sum(r.total_duration_sec for r in reports)
    i2v_sec = sum(r.i2v_duration_sec for r in reports)
    unique_imgs = sum(r.unique_images_count for r in reports)
    img_usd = sum(r.images_usd for r in reports)
    i2v_usd = sum(r.i2v_usd for r in reports)
    total_shots = sum(len(sc.shots) for sc in scenes)
    reused_shots = sum(sum(1 for s in sc.shots if s.reuse_master_bg) for sc in scenes)
    return CostReport(
        total_duration_sec=total_sec,
        i2v_duration_sec=i2v_sec,
        i2v_ratio=i2v_sec / max(total_sec, 0.001),
        unique_images_count=unique_imgs,
        images_usd=img_usd,
        i2v_usd=i2v_usd,
        total_usd=img_usd + i2v_usd,
        bank_reuse_ratio=reused_shots / max(total_shots, 1),
    )


def enforce_circuit_breakers(
    episode: EpisodeScript,
    scenes: list[SceneStoryboard],
    allow_over_ratio: bool = False,
) -> CostReport:
    """Double coupe-circuit : plafond scène + plafond épisode + ratio I2V <= 15%."""
    for sc in scenes:
        sc_report = estimate_scene_cost(sc)
        if sc_report.total_usd > sc.max_budget_usd:
            raise BudgetExceededError(
                f"Coupe-circuit scène [{sc.scene_id}] : coût estimé ({sc_report.total_usd:.2f} $) "
                f"> plafond scène ({sc.max_budget_usd:.2f} $)."
            )

    ep_report = estimate_episode_cost(scenes)
    if ep_report.total_usd > episode.max_budget_usd:
        raise BudgetExceededError(
            f"Coupe-circuit épisode [{episode.episode_id}] : coût estimé ({ep_report.total_usd:.2f} $) "
            f"> plafond épisode ({episode.max_budget_usd:.2f} $)."
        )

    if not allow_over_ratio and ep_report.i2v_ratio > episode.max_i2v_ratio:
        raise BudgetExceededError(
            f"Coupe-circuit ratio I2V [{episode.episode_id}] : "
            f"{ep_report.i2v_ratio * 100:.1f} % > plafond autorisé ({episode.max_i2v_ratio * 100:.1f} %). "
            "Convertis des plans action_i2v en parallax_2_5d/dialogue_lipsync ou utilise --allow-over-ratio."
        )
    return ep_report


def append_cost_log(
    log_path: Path,
    episode_id: str,
    scene_id: str,
    shot_id: str,
    category: Literal["image", "i2v", "tts"],
    provider: str,
    model: str,
    unit_cost_usd: float,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = CostLogEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        episode_id=episode_id,
        scene_id=scene_id,
        shot_id=shot_id,
        category=category,
        provider=provider,
        model=model,
        unit_cost_usd=round(unit_cost_usd, 4),
    )
    with log_path.open("a", encoding="utf-8") as f:
        f.write(entry.model_dump_json() + "\n")
