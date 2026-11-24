from pathlib import Path
import pytest
from PIL import Image

from src.analyzer.build_style_bible import build_style_bible
from src.audio.voice_engine import build_episode_voices, compute_mouth_states_12fps
from src.compositing.master_episode import generate_ass_subtitles
from src.generators.animate_i2v import SelectiveI2VClient
from src.generators.asset_bank import AssetBank
from src.models import (
    CharacterSpec,
    DialogueLine,
    EpisodeScript,
    MasterLocation,
    SceneScript,
    SceneStoryboard,
    SeriesBible,
    Shot,
)
from src.utils.cost_guard import (
    BudgetExceededError,
    append_cost_log,
    enforce_circuit_breakers,
)


def _demo_fixtures() -> tuple[SeriesBible, EpisodeScript, SceneStoryboard]:
    bible = SeriesBible(
        series_title="Méridien Zéro",
        characters=[
            CharacterSpec(id="kaen", name="Kaen Vael", visual_prompt="young man, amber scar", accent_color_hex="#f59e0b"),
        ],
        master_locations=[
            MasterLocation(id="opaline_ashes", name="Opaline", lighting_variants=["sunset"], background_prompt="ruins"),
        ],
    )
    ep = EpisodeScript(
        episode_id="ep01",
        episode_title="Test Ep01",
        max_budget_usd=15.5,
        max_i2v_ratio=0.15,
        scenes=[
            SceneScript(
                scene_id="SC01",
                act="cold_open",
                location_id="opaline_ashes",
                lighting="sunset",
                dialogue_lines=[
                    DialogueLine(line_id="L001", character_id="kaen", text="Nora… n'oublie pas mon nom."),
                ],
            )
        ],
    )
    sc = SceneStoryboard(
        episode_id="ep01",
        scene_id="SC01",
        act="cold_open",
        max_budget_usd=1.2,
        shots=[
            Shot(shot_id="S01", duration_sec=8.0, render_mode="static_pan", location_id="opaline_ashes", lighting="sunset", reuse_master_bg=True, scene_prompt="wide"),
            Shot(shot_id="S02", duration_sec=6.0, render_mode="dialogue_lipsync", location_id="opaline_ashes", lighting="sunset", reuse_master_bg=True, character_ids=["kaen"], line_id="L001", scene_prompt="kaen"),
            Shot(shot_id="S03", duration_sec=2.0, render_mode="action_i2v", location_id="opaline_ashes", lighting="sunset", reuse_master_bg=False, character_ids=["kaen"], scene_prompt="dash", motion_prompt="fast"),
        ],
    )
    return bible, ep, sc


def test_circuit_breakers_pass_and_block(tmp_path: Path) -> None:
    _, ep, sc = _demo_fixtures()
    report = enforce_circuit_breakers(ep, [sc])
    assert report.i2v_ratio <= 0.15
    assert report.bank_reuse_ratio >= 0.60

    over_ratio = SceneStoryboard(
        episode_id="ep01", scene_id="SC99", act="act_b", max_budget_usd=5.0,
        shots=[
            Shot(shot_id="S01", duration_sec=3.0, render_mode="action_i2v", scene_prompt="fight", reuse_master_bg=False),
            Shot(shot_id="S02", duration_sec=3.0, render_mode="static_pan", scene_prompt="bg", reuse_master_bg=True),
        ],
    )
    with pytest.raises(BudgetExceededError, match="ratio I2V"):
        enforce_circuit_breakers(ep, [over_ratio])

    log_file = tmp_path / "cost_log.jsonl"
    append_cost_log(log_file, "ep01", "SC01", "S03", "i2v", "fal", "wan-turbo", 0.10)
    assert '"unit_cost_usd":0.1' in log_file.read_text(encoding="utf-8")


def test_voice_engine_and_subtitles(tmp_path: Path) -> None:
    _, ep, sc = _demo_fixtures()
    script_path = tmp_path / "script.json"
    script_path.write_text(ep.model_dump_json(indent=2), encoding="utf-8")

    manifest = build_episode_voices(script_path, tmp_path / "voices", dry_run=True)
    wav = Path(manifest["L001"].wav_path)
    assert wav.exists()
    dur, states = compute_mouth_states_12fps(wav, fps=12)
    assert dur >= 1.0 and 2 in states

    ass = generate_ass_subtitles(ep, [sc], tmp_path / "ep01.ass")
    assert "Nora… n'oublie pas mon nom." in ass.read_text(encoding="utf-8")


def test_asset_bank_i2v_and_style_analyzer(tmp_path: Path) -> None:
    bible, _, sc = _demo_fixtures()
    bank = AssetBank({}, bible, bank_dir=tmp_path / "bank", workspace_dir=tmp_path / "ws", dry_run=True)
    assert bank.ensure_master_location(bible.master_locations[0], "sunset").exists()
    pack = bank.ensure_character_pack(bible.characters[0], emotion="neutral")
    assert pack["settei"].exists() and pack["m_open_e_open"].exists()

    i2v = SelectiveI2VClient("ep01", tmp_path / "ws", dry_run=True)
    assert i2v.build_payload(sc.shots[-1], "img")["generate_audio"] is False

    ds = tmp_path / "ds"
    (ds / "decors").mkdir(parents=True)
    Image.new("RGB", (1280, 720), (30, 40, 80)).save(ds / "decors/bg.png")
    out = build_style_bible(ds, ds / "metadata.jsonl", tmp_path / "style.json", style_name="horlo-punk")
    assert out["meta"]["total_images_indexed"] == 1
