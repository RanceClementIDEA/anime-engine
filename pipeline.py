from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> bool:
        env_file = Path(".env")
        if env_file.exists():
            import os
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        return True

from src.audio.voice_engine import build_episode_voices
from src.compositing.master_episode import (
    assemble_episode_master,
    generate_ass_subtitles,
    render_scene_master,
)
from src.generators.animate_dialogue import render_dialogue_shot
from src.generators.animate_i2v import SelectiveI2VClient
from src.generators.animate_local import render_local_shot
from src.generators.asset_bank import AssetBank
from src.models import EpisodeScript, SceneStoryboard, SeriesBible
from src.utils.cost_guard import enforce_circuit_breakers, estimate_episode_cost


def load_scene_storyboards(episode_dir: Path, only_scene: str | None = None) -> list[SceneStoryboard]:
    scenes_dir = episode_dir / "scenes"
    paths = sorted(scenes_dir.glob("SC*.json"))
    if only_scene:
        paths = [p for p in paths if p.stem.upper() == only_scene.upper()]
    if not paths:
        raise FileNotFoundError(f"Aucun storyboard trouvé dans {scenes_dir} (filtre={only_scene})")
    return [SceneStoryboard.model_validate_json(p.read_text(encoding="utf-8")) for p in paths]


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="anime-engine — Pipeline Voie A (Épisode Long 20 min)")
    parser.add_argument("--episode", type=str, default="ep01")
    parser.add_argument("--scene", type=str, default=None, help="Rendre uniquement une scène (ex. SC01)")
    parser.add_argument("--estimate-only", action="store_true", help="Affiche le budget sans rendu")
    parser.add_argument("--dry-run", action="store_true", help="Animatique complète à 0 €")
    parser.add_argument("--allow-over-ratio", action="store_true", help="Autorise > 15 % d'I2V")
    parser.add_argument("--force-shot", type=str, default=None, help="Force la régénération d'un shot_id")
    args = parser.parse_args()

    style_bible = json.loads(Path("config/style_bible.json").read_text(encoding="utf-8"))
    series_bible = SeriesBible.model_validate_json(Path("config/series_bible.json").read_text(encoding="utf-8"))
    episode_dir = Path("episodes") / args.episode
    episode_script = EpisodeScript.model_validate_json((episode_dir / "script.json").read_text(encoding="utf-8"))
    scenes = load_scene_storyboards(episode_dir, only_scene=args.scene)

    report = estimate_episode_cost(scenes)
    print(f"[{args.episode}] Estimation Voie A -> {json.dumps(report.to_dict(), ensure_ascii=False)}")

    if args.estimate_only:
        return

    if not args.dry_run:
        enforce_circuit_breakers(episode_script, scenes, allow_over_ratio=args.allow_over_ratio)

    workspace_dir = Path("workspace") / args.episode
    voices_manifest = build_episode_voices(
        episode_dir / "script.json",
        workspace_dir / "voices",
        dry_run=args.dry_run,
    )

    resolution = (1920, 1080) if series_bible.aspect_ratio == "16:9" else (1080, 1920)
    bank = AssetBank(
        style_bible=style_bible,
        series_bible=series_bible,
        bank_dir=Path("bank"),
        workspace_dir=workspace_dir,
        episode_id=args.episode,
        dry_run=args.dry_run,
    )
    i2v_client = SelectiveI2VClient(episode_id=args.episode, workspace_dir=workspace_dir, dry_run=args.dry_run)

    rendered_scenes: list[Path] = []
    for scene in scenes:
        scene_clips: list[Path] = []
        for shot in scene.shots:
            clip_path = workspace_dir / "shots" / f"{scene.scene_id}_{shot.shot_id}.mp4"
            if args.force_shot and shot.shot_id == args.force_shot and clip_path.exists():
                clip_path.unlink()

            start_frame = bank.ensure_shot_start_frame(scene.scene_id, shot, resolution=resolution)

            if shot.render_mode == "dialogue_lipsync" and shot.character_ids:
                char = series_bible.get_character(shot.character_ids[0])
                if char is None:
                    raise ValueError(f"Personnage inconnu dans {shot.shot_id}: {shot.character_ids[0]}")
                sprite_pack = bank.ensure_character_pack(char, emotion=shot.emotion)
                voice_entry = voices_manifest.get(shot.line_id) if shot.line_id else None
                render_dialogue_shot(shot, start_frame, sprite_pack, voice_entry, clip_path, resolution=resolution)
            elif shot.render_mode == "action_i2v":
                i2v_client.render_hero_shot(scene.scene_id, shot, start_frame, clip_path, resolution=resolution)
            else:
                fg_rgba = None
                if shot.render_mode == "parallax_2_5d" and shot.character_ids:
                    char = series_bible.get_character(shot.character_ids[0])
                    if char:
                        fg_rgba = bank.ensure_character_pack(char, emotion=shot.emotion)["m_closed_e_open"]
                render_local_shot(shot, start_frame, clip_path, foreground_rgba=fg_rgba, resolution=resolution)

            scene_clips.append(clip_path)

        bgm_candidate = Path("bank/audio_sfx") / f"bgm_{scene.bgm_mood}.mp3"
        scene_mp4 = workspace_dir / "scenes_rendered" / f"{scene.scene_id}.mp4"
        render_scene_master(
            scene_id=scene.scene_id,
            clip_paths=scene_clips,
            out_scene_mp4=scene_mp4,
            style_bible=style_bible,
            bgm_path=bgm_candidate if bgm_candidate.exists() else None,
        )
        rendered_scenes.append(scene_mp4)

    out_ass = generate_ass_subtitles(episode_script, scenes, Path("output") / f"{args.episode}_subtitles.ass")
    out_mp4 = assemble_episode_master(rendered_scenes, Path("output") / f"{args.episode}_master.mp4")
    print(f"Master généré : {out_mp4}")
    print(f"Sous-titres   : {out_ass}")


if __name__ == "__main__":
    main()
