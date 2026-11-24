from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field

RenderMode = Literal[
    "dialogue_lipsync",
    "static_pan",
    "parallax_2_5d",
    "insert_cut",
    "impact_frame",
    "action_i2v",
]
CameraMove = Literal["none", "zoom_in", "zoom_out", "pan_left", "pan_right", "shake"]
LightingType = Literal["day", "sunset", "night"]
ActType = Literal["cold_open", "act_a", "act_b", "preview"]


class CharacterSpec(BaseModel):
    id: str
    name: str
    role: str = "main"
    voice_profile: str = "neutral_fr"
    visual_prompt: str
    expressions: list[str] = Field(
        default_factory=lambda: ["neutral", "angry", "shocked", "sad", "smirk"]
    )
    accent_color_hex: str = "#38bdf8"


class MasterLocation(BaseModel):
    id: str
    name: str
    lighting_variants: list[LightingType] = Field(default_factory=lambda: ["day", "night"])
    background_prompt: str


class SeriesBible(BaseModel):
    series_title: str
    genre: str = "shonen"
    aspect_ratio: Literal["16:9", "9:16"] = "16:9"
    characters: list[CharacterSpec]
    master_locations: list[MasterLocation]
    recurring_props: list[str] = Field(default_factory=list)
    audio_identity: dict[str, str] = Field(default_factory=dict)

    def get_character(self, char_id: str) -> CharacterSpec | None:
        return next((c for c in self.characters if c.id == char_id), None)

    def get_location(self, location_id: str) -> MasterLocation | None:
        return next((loc for loc in self.master_locations if loc.id == location_id), None)


class DialogueLine(BaseModel):
    line_id: str
    character_id: str | None = None
    emotion: str = "neutral"
    text: str
    pause_after_sec: float = Field(default=0.4, ge=0.0, le=5.0)
    action_note: str = ""


class SceneScript(BaseModel):
    scene_id: str
    act: ActType
    location_id: str
    lighting: LightingType = "night"
    bgm_mood: str = "tension"
    is_sakuga_priority: bool = False
    estimated_duration_sec: float = Field(default=60.0, gt=0.0)
    summary: str = ""
    dialogue_lines: list[DialogueLine] = Field(default_factory=list)


class EpisodeScript(BaseModel):
    episode_id: str
    episode_title: str
    max_budget_usd: float = Field(default=16.0, ge=0.0)
    max_i2v_ratio: float = Field(default=0.15, ge=0.0, le=1.0)
    scenes: list[SceneScript]


class Shot(BaseModel):
    shot_id: str = Field(..., examples=["S01"])
    duration_sec: float = Field(..., ge=0.08, le=10.0)
    render_mode: RenderMode
    camera_move: CameraMove = "zoom_in"
    location_id: str | None = None
    lighting: LightingType = "night"
    reuse_master_bg: bool = True
    character_ids: list[str] = Field(default_factory=list)
    emotion: str = "neutral"
    line_id: str | None = None
    scene_prompt: str
    motion_prompt: str = ""
    sfx_cues: list[str] = Field(default_factory=list)


class SceneStoryboard(BaseModel):
    episode_id: str
    scene_id: str
    act: ActType
    bgm_mood: str = "tension"
    max_budget_usd: float = Field(default=2.0, ge=0.0)
    shots: list[Shot]


class VoiceDurationEntry(BaseModel):
    line_id: str
    character_id: str | None = None
    wav_path: str
    duration_sec: float
    fps: int = 12
    mouth_states_12fps: list[int] = Field(default_factory=list)


class CostLogEntry(BaseModel):
    timestamp: str
    episode_id: str
    scene_id: str
    shot_id: str
    category: Literal["image", "i2v", "tts"]
    provider: str
    model: str
    unit_cost_usd: float
