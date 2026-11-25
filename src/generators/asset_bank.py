from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw
from src.models import CharacterSpec, LightingType, MasterLocation, SeriesBible, Shot
from src.utils.cost_guard import ESTIMATED_IMG_USD, append_cost_log


def _hex_to_rgb(hex_color: str, default: tuple[int, int, int] = (56, 189, 248)) -> tuple[int, int, int]:
    cleaned = hex_color.lstrip("#")
    if len(cleaned) != 6:
        return default
    return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))


class AssetBank:
    """Gère le Bank System idempotent : décors maîtres 2560x1440, settei et sprites RGBA."""

    def __init__(
        self,
        style_bible: dict[str, Any],
        series_bible: SeriesBible,
        bank_dir: Path = Path("bank"),
        workspace_dir: Path = Path("workspace/ep01"),
        episode_id: str = "ep01",
        dry_run: bool = False,
    ) -> None:
        self.style_bible = style_bible
        self.series_bible = series_bible
        self.bank_dir = bank_dir
        self.workspace_dir = workspace_dir
        self.episode_id = episode_id
        self.dry_run = dry_run
        self.prefix = style_bible.get("prompt_templates", {}).get("positive_prefix", "japanese anime style")
        self.cost_log_path = workspace_dir / "cost_log.jsonl"

    def _use_placeholder(self) -> bool:
        if self.dry_run:
            return True
        provider = os.getenv("IMAGE_API_PROVIDER", "fal").lower()
        if provider == "pollinations":
            return False
        return not bool(os.getenv("IMAGE_API_KEY", "").strip())

    def ensure_master_location(self, location: MasterLocation, lighting: LightingType) -> Path:
        target = self.bank_dir / "locations" / f"{location.id}_{lighting}.png"
        if target.exists() and target.stat().st_size > 0:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)

        if self._use_placeholder():
            self._draw_placeholder_bg(target, f"{location.name} ({lighting})", lighting)
            return target

        prompt = f"{self.prefix}, anime background art, no characters, {lighting} lighting, {location.background_prompt}"
        return self._call_image_api(prompt, target, size=(2560, 1440), scene_id="BANK", shot_id=f"loc_{location.id}")

    def ensure_character_pack(self, char: CharacterSpec, emotion: str = "neutral") -> dict[str, Path]:
        """Garantit settei.png + 6 variantes RGBA (3 bouches x 2 états d'yeux) pour le Kuchipaku."""
        char_dir = self.bank_dir / "characters" / char.id
        char_dir.mkdir(parents=True, exist_ok=True)
        settei_path = char_dir / "settei.png"
        if not (settei_path.exists() and settei_path.stat().st_size > 0):
            if self._use_placeholder():
                self._draw_placeholder_settei(settei_path, char)
            else:
                prompt = f"{self.prefix}, character sheet, front side back views, white background, {char.visual_prompt}"
                self._call_image_api(prompt, settei_path, size=(1344, 768), scene_id="BANK", shot_id=f"settei_{char.id}")

        pack: dict[str, Path] = {"settei": settei_path}
        for mouth in ("closed", "half", "open"):
            for eyes in ("open", "closed"):
                key = f"{emotion}_m_{mouth}_e_{eyes}"
                sprite_path = char_dir / f"{key}.png"
                if not (sprite_path.exists() and sprite_path.stat().st_size > 0):
                    self._draw_placeholder_character_sprite(sprite_path, char, emotion, mouth, eyes)
                pack[f"m_{mouth}_e_{eyes}"] = sprite_path
        return pack

    def ensure_shot_start_frame(
        self,
        scene_id: str,
        shot: Shot,
        resolution: tuple[int, int] = (1920, 1080),
    ) -> Path:
        if shot.reuse_master_bg and shot.location_id and shot.render_mode in {"static_pan", "dialogue_lipsync"}:
            loc = self.series_bible.get_location(shot.location_id)
            if loc:
                return self.ensure_master_location(loc, shot.lighting)

        target = self.workspace_dir / "frames" / f"{scene_id}_{shot.shot_id}_start.png"
        if target.exists() and target.stat().st_size > 0:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)

        if self._use_placeholder():
            self._draw_placeholder_shot(target, scene_id, shot, resolution)
            return target

        prompt = f"{self.prefix}, {shot.scene_prompt}"
        return self._call_image_api(prompt, target, size=resolution, scene_id=scene_id, shot_id=shot.shot_id)

    def _call_image_api(
        self,
        prompt: str,
        target: Path,
        size: tuple[int, int],
        scene_id: str,
        shot_id: str,
    ) -> Path:
        provider = os.getenv("IMAGE_API_PROVIDER", "fal").lower()
        gen_w, gen_h = (1280, 720) if size[0] >= size[1] else (720, 1280)

        if provider == "pollinations":
            return self._call_pollinations(prompt, target, size, (gen_w, gen_h), scene_id, shot_id)

        if provider != "fal":
            raise ValueError(f"IMAGE_API_PROVIDER non supporté : {provider} (attendu: fal ou pollinations)")

        api_key = os.getenv("IMAGE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("IMAGE_API_KEY manquante dans .env")

        model = os.getenv("FAL_IMAGE_MODEL", "fal-ai/flux/dev")
        headers = {
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "prompt": prompt,
            "image_size": {"width": 1344 if size[0] >= size[1] else 768, "height": 768 if size[0] >= size[1] else 1344},
            "num_images": 1,
            "enable_safety_checker": False,
        }

        print(f"[fal.ai image] {scene_id}_{shot_id} -> {model}...")
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(f"https://fal.run/{model}", headers=headers, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"Erreur fal.ai ({resp.status_code}) : {resp.text[:300]}")
            data = resp.json()
            images = data.get("images") or []
            if not images or not images[0].get("url"):
                raise RuntimeError(f"Réponse fal.ai sans URL d'image : {data}")
            img_resp = client.get(images[0]["url"])
            img_resp.raise_for_status()

        self._save_resized_png(img_resp.content, target, size)
        append_cost_log(
            self.cost_log_path,
            episode_id=self.episode_id,
            scene_id=scene_id,
            shot_id=shot_id,
            category="image",
            provider=provider,
            model=model,
            unit_cost_usd=ESTIMATED_IMG_USD,
        )
        return target

    def _call_pollinations(
        self,
        prompt: str,
        target: Path,
        size: tuple[int, int],
        gen_size: tuple[int, int],
        scene_id: str,
        shot_id: str,
    ) -> Path:
        model = os.getenv("POLLINATIONS_MODEL", "flux")
        seed = int(hashlib.md5(f"{scene_id}_{shot_id}".encode("utf-8")).hexdigest()[:8], 16) % 100000
        encoded_prompt = quote(prompt[:900], safe="")
        api_key = os.getenv("POLLINATIONS_API_KEY", "").strip()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        url = (
            f"https://gen.pollinations.ai/image/{encoded_prompt}"
            f"?width={gen_size[0]}&height={gen_size[1]}&seed={seed}&model={model}"
        )

        print(f"[pollinations 0€] {scene_id}_{shot_id} -> {model}...")
        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                with httpx.Client(timeout=120.0, follow_redirects=True) as client:
                    resp = client.get(url, headers=headers)
                    if resp.status_code in {429, 502, 503, 504}:
                        time.sleep(3.0 * attempt)
                        continue
                    resp.raise_for_status()
                    self._save_resized_png(resp.content, target, size)
                    append_cost_log(
                        self.cost_log_path,
                        episode_id=self.episode_id,
                        scene_id=scene_id,
                        shot_id=shot_id,
                        category="image",
                        provider="pollinations",
                        model=model,
                        unit_cost_usd=0.0,
                    )
                    time.sleep(1.5)
                    return target
            except Exception as exc:
                last_err = exc
                time.sleep(3.0 * attempt)

        raise RuntimeError(f"Échec Pollinations sur {scene_id}_{shot_id} après 3 essais : {last_err}")

    @staticmethod
    def _save_resized_png(raw_bytes: bytes, target: Path, size: tuple[int, int]) -> None:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            rgb = im.convert("RGB")
            if rgb.size != size:
                rgb = rgb.resize(size, Image.Resampling.LANCZOS)
            target.parent.mkdir(parents=True, exist_ok=True)
            rgb.save(target, format="PNG")

    @staticmethod
    def _draw_placeholder_bg(path: Path, label: str, lighting: LightingType) -> None:
        palettes = {
            "day": ((110, 165, 225), (45, 70, 110)),
            "sunset": ((215, 105, 85), (58, 34, 66)),
            "night": ((18, 24, 48), (10, 12, 24)),
        }
        top_c, bot_c = palettes.get(lighting, palettes["night"])
        w, h = 2560, 1440
        img = Image.new("RGB", (w, h), top_c)
        draw = ImageDraw.Draw(img)
        for y in range(0, h, 8):
            ratio = y / h
            col = tuple(int(top_c[i] * (1 - ratio) + bot_c[i] * ratio) for i in range(3))
            draw.rectangle([0, y, w, y + 8], fill=col)
        draw.rectangle([0, int(h * 0.68), w, h], fill=(16, 18, 30))
        for bx in range(80, w - 120, 220):
            bh = 220 + (bx * 37) % 340
            draw.rectangle([bx, int(h * 0.68) - bh, bx + 160, int(h * 0.68)], fill=(26, 30, 52), outline=(65, 78, 120), width=3)
        draw.text((80, 60), f"MASTER BG [2560x1440] :: {label}", fill=(240, 244, 255))
        img.save(path)

    @staticmethod
    def _draw_placeholder_settei(path: Path, char: CharacterSpec) -> None:
        img = Image.new("RGB", (1344, 768), (242, 244, 248))
        draw = ImageDraw.Draw(img)
        accent = _hex_to_rgb(char.accent_color_hex)
        draw.rectangle([24, 24, 1320, 744], outline=(30, 41, 59), width=4)
        for idx, view in enumerate(("FRONT", "SIDE", "BACK")):
            cx = 260 + idx * 410
            draw.ellipse([cx - 55, 160, cx + 55, 270], fill=(248, 224, 200), outline=(20, 20, 30), width=4)
            draw.polygon([(cx - 70, 280), (cx + 70, 280), (cx + 90, 620), (cx - 90, 620)], fill=accent, outline=(20, 20, 30))
            draw.text((cx - 25, 650), view, fill=(15, 23, 42))
        draw.text((55, 50), f"SETTEI :: {char.name} ({char.id}) - {char.visual_prompt[:75]}", fill=(15, 23, 42))
        img.save(path)

    @staticmethod
    def _draw_placeholder_character_sprite(
        path: Path,
        char: CharacterSpec,
        emotion: str,
        mouth: str,
        eyes: str,
    ) -> None:
        w, h = 900, 1080
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        accent = _hex_to_rgb(char.accent_color_hex) + (255,)
        outline = (18, 22, 34, 255)
        skin = (250, 224, 202, 255)

        draw.polygon([(220, 1080), (275, 540), (625, 540), (680, 1080)], fill=accent, outline=outline)
        draw.rectangle([410, 470, 490, 560], fill=skin, outline=outline, width=4)
        draw.ellipse([300, 170, 600, 495], fill=skin, outline=outline, width=5)
        draw.pieslice([285, 130, 615, 430], 175, 365, fill=(28, 34, 54, 255), outline=outline, width=4)

        if eyes == "open":
            draw.ellipse([355, 300, 415, 345], fill=(255, 255, 255, 255), outline=outline, width=4)
            draw.ellipse([485, 300, 545, 345], fill=(255, 255, 255, 255), outline=outline, width=4)
            draw.ellipse([375, 310, 400, 338], fill=accent)
            draw.ellipse([505, 310, 530, 338], fill=accent)
        else:
            draw.line([(355, 325), (415, 325)], fill=outline, width=6)
            draw.line([(485, 325), (545, 325)], fill=outline, width=6)

        if mouth == "closed":
            draw.line([(415, 420), (485, 420)], fill=outline, width=5)
        elif mouth == "half":
            draw.ellipse([420, 408, 480, 436], fill=(185, 65, 80, 255), outline=outline, width=4)
        else:
            draw.ellipse([415, 398, 485, 452], fill=(155, 35, 55, 255), outline=outline, width=4)

        draw.text((310, 600), f"{char.name} [{emotion}]", fill=(255, 255, 255, 230))
        img.save(path)

    @staticmethod
    def _draw_placeholder_shot(path: Path, scene_id: str, shot: Shot, resolution: tuple[int, int]) -> None:
        w, h = resolution
        img = Image.new("RGB", (w, h), (22, 27, 44))
        draw = ImageDraw.Draw(img)
        border = (225, 29, 72) if shot.render_mode == "action_i2v" else (56, 189, 248)
        draw.rectangle([36, 36, w - 36, h - 36], outline=border, width=6)
        draw.line([(0, h // 2), (w, h // 2)], fill=(45, 55, 82), width=2)
        draw.line([(w // 2, 0), (w // 2, h)], fill=(45, 55, 82), width=2)
        draw.text((70, 80), f"{scene_id}_{shot.shot_id} | mode={shot.render_mode} | move={shot.camera_move} | {shot.duration_sec:.2f}s", fill=(248, 250, 252))
        draw.text((70, 130), f"PROMPT: {shot.scene_prompt[:120]}", fill=(186, 200, 224))
        if shot.motion_prompt:
            draw.text((70, 175), f"MOTION: {shot.motion_prompt[:120]}", fill=(251, 146, 60))
        img.save(path)
