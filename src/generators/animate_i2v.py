from __future__ import annotations

import os
from pathlib import Path

from src.generators.animate_local import render_local_shot
from src.models import Shot
from src.utils.cost_guard import ESTIMATED_I2V_USD_PER_SEC, append_cost_log


class SelectiveI2VClient:
    """Client I2V sélectif (Voie A) avec generate_audio=False codé en dur."""

    GENERATE_AUDIO: bool = False  # Toujours désactivé pour diviser le coût par 2

    def __init__(self, episode_id: str, workspace_dir: Path, dry_run: bool = False) -> None:
        self.episode_id = episode_id
        self.cost_log_path = workspace_dir / "cost_log.jsonl"
        self.dry_run = dry_run
        self.api_key = os.getenv("I2V_API_KEY", "")
        self.provider = os.getenv("I2V_API_PROVIDER", "fal")

    def build_payload(self, shot: Shot, image_url_or_b64: str) -> dict[str, object]:
        return {
            "image": image_url_or_b64,
            "prompt": shot.motion_prompt or shot.scene_prompt,
            "duration": min(5, max(2, int(round(shot.duration_sec)))),
            "generate_audio": self.GENERATE_AUDIO,
        }

    def render_hero_shot(
        self,
        scene_id: str,
        shot: Shot,
        start_frame: Path,
        out_clip: Path,
        resolution: tuple[int, int] = (1920, 1080),
    ) -> Path:
        if out_clip.exists() and out_clip.stat().st_size > 0:
            return out_clip

        if self.dry_run or not self.api_key or self.provider in {"local", "none", "pollinations"}:
            return render_local_shot(shot, start_frame, out_clip, resolution=resolution)

        append_cost_log(
            self.cost_log_path,
            episode_id=self.episode_id,
            scene_id=scene_id,
            shot_id=shot.shot_id,
            category="i2v",
            provider=self.provider,
            model="i2v-turbo-no-audio",
            unit_cost_usd=shot.duration_sec * ESTIMATED_I2V_USD_PER_SEC,
        )
        raise NotImplementedError(
            f"Branche ici l'appel httpx vers {self.provider} en utilisant build_payload() (generate_audio=False)."
        )
