from __future__ import annotations

import base64
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
    """Gère le Bank System idempotent : décors maîtres 2560x1440, settei et sprites RGBA détourés (rembg)."""

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
        templates = style_bible.get("prompt_templates", {})
        self.prefix = templates.get("positive_prefix", "japanese anime style")
        self.bg_suffix = templates.get("background_suffix", "anime background art, no characters")
        self.char_lineart = templates.get("character_lineart", "anime production settei quality, crisp ink lineart")
        self.acting_closeup = templates.get("acting_closeup", "sakuga character acting keyframe")
        self.cost_log_path = workspace_dir / "cost_log.jsonl"
        self._rembg_session: Any = None

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

        prompt = f"{self.prefix}, {self.bg_suffix}, {lighting} lighting, {location.background_prompt}"
        return self._call_image_api(prompt, target, size=(2560, 1440), scene_id="BANK", shot_id=f"loc_{location.id}")

    def ensure_character_pack(self, char: CharacterSpec, emotion: str = "neutral") -> dict[str, Path]:
        """Garantit settei.png + 6 variantes RGBA (3 bouches x 2 états d'yeux) détourées via rembg."""
        char_dir = self.bank_dir / "characters" / char.id
        char_dir.mkdir(parents=True, exist_ok=True)
        settei_path = char_dir / "settei.png"

        if not (settei_path.exists() and settei_path.stat().st_size > 0):
            if self._use_placeholder():
                self._draw_placeholder_settei(settei_path, char)
            else:
                prompt = (
                    f"{self.prefix}, {self.char_lineart}, character reference sheet, "
                    f"front side back views, solid pure white background, {char.visual_prompt}"
                )
                self._call_image_api(prompt, settei_path, size=(1344, 768), scene_id="BANK", shot_id=f"settei_{char.id}")

        pack: dict[str, Path] = {"settei": settei_path}

        if self._use_placeholder():
            for mouth in ("closed", "half", "open"):
                for eyes in ("open", "closed"):
                    sprite_path = char_dir / f"{emotion}_m_{mouth}_e_{eyes}.png"
                    if not (sprite_path.exists() and sprite_path.stat().st_size > 0):
                        self._draw_placeholder_character_sprite(sprite_path, char, emotion, mouth, eyes)
                    pack[f"m_{mouth}_e_{eyes}"] = sprite_path
            return pack

        # Mode réel (Pollinations ou fal.ai) : génère 1 buste ancré sur le settei + détourage RGBA rembg
        base_raw = char_dir / f"{emotion}_base_raw.png"
        base_rgba = char_dir / f"{emotion}_base_rgba.png"

        if not (base_raw.exists() and base_raw.stat().st_size > 0):
            bust_prompt = (
                f"{self.prefix}, {self.char_lineart}, {self.acting_closeup}, "
                f"single character upper body bust portrait facing slightly front, {emotion} expression, "
                f"eyes open, mouth closed, isolated on solid flat pure white background, no background elements, "
                f"{char.visual_prompt}"
            )
            if os.getenv("IMAGE_API_PROVIDER", "fal").lower() == "fal" and settei_path.exists():
                self._call_fal_kontext_or_fallback(
                    prompt=(
                        f"Extract this exact character from the reference sheet as a clean upper-body anime bust portrait, "
                        f"{emotion} facial expression, eyes open, mouth closed, isolated on a solid pure white background, "
                        f"{self.char_lineart}"
                    ),
                    reference_image=settei_path,
                    target=base_raw,
                    size=(900, 1080),
                    scene_id="BANK",
                    shot_id=f"bust_{char.id}_{emotion}",
                    fallback_prompt=bust_prompt,
                )
            else:
                self._call_image_api(
                    bust_prompt,
                    base_raw,
                    size=(900, 1080),
                    scene_id="BANK",
                    shot_id=f"bust_{char.id}_{emotion}",
                )

        if not (base_rgba.exists() and base_rgba.stat().st_size > 0):
            self._cutout_to_rgba_sprite(base_raw, base_rgba, canvas_size=(900, 1080))

        sprite_mode = os.getenv("SPRITE_GENERATION_MODE", "base_rembg").lower()
        use_kontext_variants = (
            sprite_mode == "kontext_6"
            and os.getenv("IMAGE_API_PROVIDER", "fal").lower() == "fal"
            and bool(os.getenv("IMAGE_API_KEY", "").strip())
        )

        for mouth in ("closed", "half", "open"):
            for eyes in ("open", "closed"):
                sprite_path = char_dir / f"{emotion}_m_{mouth}_e_{eyes}.png"
                if not (sprite_path.exists() and sprite_path.stat().st_size > 0):
                    if mouth == "closed" and eyes == "open":
                        sprite_path.write_bytes(base_rgba.read_bytes())
                    elif use_kontext_variants:
                        var_raw = char_dir / f"{emotion}_m_{mouth}_e_{eyes}_raw.png"
                        mouth_desc = {
                            "closed": "mouth completely closed",
                            "half": "mouth slightly open speaking",
                            "open": "mouth wide open speaking",
                        }[mouth]
                        eyes_desc = "eyes open" if eyes == "open" else "eyes gently closed blinking"
                        edit_prompt = (
                            f"Keep the exact same character, pose, framing, lighting, and pure white background. "
                            f"Change ONLY the face expression: {mouth_desc}, {eyes_desc}."
                        )
                        self._call_fal_kontext_or_fallback(
                            prompt=edit_prompt,
                            reference_image=base_raw,
                            target=var_raw,
                            size=(900, 1080),
                            scene_id="BANK",
                            shot_id=f"sprite_{char.id}_{emotion}_{mouth}_{eyes}",
                            fallback_prompt=None,
                        )
                        self._cutout_to_rgba_sprite(var_raw, sprite_path, canvas_size=(900, 1080))
                    else:
                        self._derive_locked_variant(base_rgba, sprite_path, mouth=mouth, eyes=eyes)

                pack[f"m_{mouth}_e_{eyes}"] = sprite_path

        return pack

    def _get_rembg_session(self) -> Any:
        if self._rembg_session is None:
            from rembg import new_session

            model_name = os.getenv("REMBG_MODEL", "isnet-anime")
            print(f"[rembg] Chargement du modele de detourage '{model_name}'...")
            self._rembg_session = new_session(model_name)
        return self._rembg_session

    def _cutout_to_rgba_sprite(
        self,
        src_path: Path,
        out_rgba_path: Path,
        canvas_size: tuple[int, int] = (900, 1080),
    ) -> Path:
        """Détoure un portrait personnage en RGBA via rembg (isnet-anime) et le cadre proprement."""
        raw_bytes = src_path.read_bytes()
        try:
            from rembg import remove

            session = self._get_rembg_session()
            cutout_bytes = remove(
                raw_bytes,
                session=session,
                alpha_matting=False,
                post_process_mask=True,
            )
            cutout = Image.open(io.BytesIO(cutout_bytes)).convert("RGBA")
        except ImportError:
            print('[rembg] Package non installe -> fallback fond blanc (pip install "rembg[cpu]" onnxruntime)')
            cutout = self._fallback_remove_white_bg(Image.open(io.BytesIO(raw_bytes)).convert("RGBA"))
        except Exception as exc:
            print(f"[rembg] Erreur detourage ({exc}) -> fallback fond blanc")
            cutout = self._fallback_remove_white_bg(Image.open(io.BytesIO(raw_bytes)).convert("RGBA"))

        alpha = cutout.getchannel("A").point(lambda p: 0 if p < 24 else p)
        cutout.putalpha(alpha)
        bbox = cutout.getbbox()
        if bbox:
            cutout = cutout.crop(bbox)

        cw, ch = canvas_size
        target_h = int(ch * 0.94)
        scale = min((cw * 0.92) / max(cutout.width, 1), target_h / max(cutout.height, 1))
        new_w = max(1, int(cutout.width * scale))
        new_h = max(1, int(cutout.height * scale))
        resized = cutout.resize((new_w, new_h), Image.Resampling.LANCZOS)

        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        pos_x = (cw - new_w) // 2
        pos_y = ch - new_h
        canvas.paste(resized, (pos_x, pos_y), resized)

        out_rgba_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_rgba_path, format="PNG")
        return out_rgba_path

    @staticmethod
    def _fallback_remove_white_bg(img: Image.Image, threshold: int = 242) -> Image.Image:
        """Fallback léger si rembg n'est pas installé : rend transparents les pixels blancs unis."""
        rgba = img.convert("RGBA")
        pixels = list(rgba.getdata())
        cleaned = [
            (r, g, b, 0) if (r >= threshold and g >= threshold and b >= threshold) else (r, g, b, a)
            for (r, g, b, a) in pixels
        ]
        rgba.putdata(cleaned)
        return rgba

    @staticmethod
    def _derive_locked_variant(base_rgba: Path, out_path: Path, mouth: str, eyes: str) -> None:
        """Crée une variante verrouillée sans jitter (micro-amplitude verticale sur la parole)."""
        with Image.open(base_rgba) as im:
            rgba = im.convert("RGBA")
            w, h = rgba.size
            dy = {"closed": 0, "half": -2, "open": -4}.get(mouth, 0)
            if eyes == "closed":
                dy += 1
            if dy == 0:
                rgba.save(out_path, format="PNG")
                return
            shifted = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            shifted.paste(rgba, (0, dy), rgba)
            shifted.save(out_path, format="PNG")

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

    def _call_fal_kontext_or_fallback(
        self,
        prompt: str,
        reference_image: Path,
        target: Path,
        size: tuple[int, int],
        scene_id: str,
        shot_id: str,
        fallback_prompt: str | None,
    ) -> Path:
        api_key = os.getenv("IMAGE_API_KEY", "").strip()
        model = os.getenv("FAL_KONTEXT_MODEL", "fal-ai/flux-pro/kontext")
        b64 = base64.b64encode(reference_image.read_bytes()).decode("ascii")
        data_uri = f"data:image/png;base64,{b64}"

        headers = {
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "prompt": prompt,
            "image_url": data_uri,
            "aspect_ratio": "3:4" if size[1] > size[0] else "16:9",
            "output_format": "png",
        }

        print(f"[fal.ai kontext] {scene_id}_{shot_id} -> {model}...")
        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(f"https://fal.run/{model}", headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                images = data.get("images") or []
                if not images or not images[0].get("url"):
                    raise RuntimeError(f"Réponse fal.ai Kontext sans image : {data}")
                img_resp = client.get(images[0]["url"])
                img_resp.raise_for_status()

            self._save_resized_png(img_resp.content, target, size)
            append_cost_log(
                self.cost_log_path,
                episode_id=self.episode_id,
                scene_id=scene_id,
                shot_id=shot_id,
                category="image",
                provider="fal",
                model=model,
                unit_cost_usd=ESTIMATED_IMG_USD,
            )
            return target
        except Exception as exc:
            if fallback_prompt:
                print(f"[fal.ai kontext] Fallback sur text-to-image ({exc})")
                return self._call_image_api(fallback_prompt, target, size=size, scene_id=scene_id, shot_id=shot_id)
            raise

    def _call_image_api(
        self,
        prompt: str,
        target: Path,
        size: tuple[int, int],
        scene_id: str,
        shot_id: str,
    ) -> Path:
        provider = os.getenv("IMAGE_API_PROVIDER", "fal").lower()
        gen_w, gen_h = (1280, 720) if size[0] >= size[1] else (768, 1024)

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
            "image_size": {"width": 1344 if size[0] >= size[1] else 768, "height": 768 if size[0] >= size[1] else 1024},
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