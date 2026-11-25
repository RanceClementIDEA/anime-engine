from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

from src.models import DialogueLine, EpisodeScript, VoiceDurationEntry

SAMPLE_RATE = 48000
LIPSYNC_FPS = 12

# Profils vocaux japonais gratuits (Microsoft Edge Neural TTS)
JA_VOICE_PROFILES: dict[str, dict[str, str]] = {
    "kaen": {"voice": "ja-JP-KeitaNeural", "rate": "+2%", "pitch": "+2Hz"},
    "mina": {"voice": "ja-JP-NanamiNeural", "rate": "+8%", "pitch": "+12Hz"},
    "bram": {"voice": "ja-JP-KeitaNeural", "rate": "-8%", "pitch": "-22Hz"},
    "lyra": {"voice": "ja-JP-NanamiNeural", "rate": "-4%", "pitch": "-8Hz"},
    "sorai": {"voice": "ja-JP-NanamiNeural", "rate": "+0%", "pitch": "-2Hz"},
    "nora": {"voice": "ja-JP-NanamiNeural", "rate": "-10%", "pitch": "+14Hz"},
    "cassian": {"voice": "ja-JP-KeitaNeural", "rate": "-6%", "pitch": "-14Hz"},
    "default": {"voice": "ja-JP-KeitaNeural", "rate": "+0%", "pitch": "-4Hz"},
}


def _synthesize_placeholder_wav(text: str, out_wav: Path, base_freq: float = 220.0) -> None:
    """Génère un .wav 48 kHz modulé par syllabes pour animatique 0 € hors-ligne."""
    words = [w for w in text.split() if w.strip()]
    duration_sec = max(1.0, min(8.0, len(words) * 0.36 + 0.35))
    n_samples = int(duration_sec * SAMPLE_RATE)

    frames = bytearray()
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        syllable_env = max(0.0, math.sin(2.0 * math.pi * 4.2 * t))
        word_gate = 0.15 if (int(t * 2.2) % 4 == 3) else 1.0
        amp = 0.28 * syllable_env * word_gate
        val = int(32767 * amp * math.sin(2.0 * math.pi * base_freq * t))
        frames.extend(struct.pack("<h", max(-32768, min(32767, val))))

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(frames))


def _synthesize_edge_tts_ja(line: DialogueLine, out_wav: Path) -> None:
    """Synthèse vocale japonaise neurale gratuite via edge-tts -> WAV 48 kHz mono."""
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "Le package edge-tts est manquant. Lance : pip install edge-tts"
        ) from exc

    spoken_text = (line.text_ja or line.text).strip()
    profile = JA_VOICE_PROFILES.get(line.character_id or "default", JA_VOICE_PROFILES["default"])

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_mp3 = Path(tmp.name)

    try:
        async def _save() -> None:
            comm = edge_tts.Communicate(
                spoken_text,
                voice=profile["voice"],
                rate=profile["rate"],
                pitch=profile["pitch"],
            )
            await comm.save(str(tmp_mp3))

        asyncio.run(_save())
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(tmp_mp3),
                "-ar", str(SAMPLE_RATE),
                "-ac", "1",
                "-c:a", "pcm_s16le",
                str(out_wav),
            ],
            check=True,
        )
    finally:
        tmp_mp3.unlink(missing_ok=True)


def compute_mouth_states_12fps(wav_path: Path, fps: int = LIPSYNC_FPS) -> tuple[float, list[int]]:
    """Calcule la durée exacte et les états de bouche (0=closed, 1=half, 2=open) à 12 fps."""
    with wave.open(str(wav_path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sampwidth != 2:
        raise ValueError(f"Format WAV 16-bit PCM attendu pour {wav_path}")

    total_samples = len(raw) // 2
    samples = struct.unpack(f"<{total_samples}h", raw)
    if n_channels > 1:
        samples = samples[::n_channels]

    duration_sec = len(samples) / max(framerate, 1)
    chunk_size = max(1, int(framerate / fps))

    rms_values: list[float] = []
    for start in range(0, len(samples), chunk_size):
        chunk = samples[start : start + chunk_size]
        if not chunk:
            continue
        mean_sq = sum(s * s for s in chunk) / len(chunk)
        rms_values.append(math.sqrt(mean_sq) / 32768.0)

    peak = max(rms_values) if rms_values else 0.0
    if peak < 1e-4:
        return round(duration_sec, 3), [0 for _ in rms_values]

    states: list[int] = []
    for val in rms_values:
        norm = val / peak
        if norm < 0.16:
            states.append(0)
        elif norm < 0.52:
            states.append(1)
        else:
            states.append(2)

    return round(duration_sec, 3), states


def _generate_line_audio(
    line: DialogueLine,
    out_wav: Path,
    dry_run: bool,
    provider_override: str | None = None,
    force: bool = False,
) -> None:
    if not force and out_wav.exists() and out_wav.stat().st_size > 44:
        return

    if provider_override:
        provider = provider_override.lower()
    elif dry_run:
        provider = "dry_run"
    else:
        provider = os.getenv("TTS_PROVIDER", "local").lower()

    if provider in {"dry_run", "local"}:
        freq = 185.0 + (abs(hash(line.character_id or "narrator")) % 140)
        _synthesize_placeholder_wav(line.text_ja or line.text, out_wav, base_freq=freq)
        return

    if provider == "edge_tts":
        _synthesize_edge_tts_ja(line, out_wav)
        return

    if provider == "elevenlabs":
        api_key = os.getenv("ELEVENLABS_API_KEY", "")
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY manquante dans .env")
        raise NotImplementedError("Branche ici ton appel httpx vers ElevenLabs (eleven_multilingual_v2 + line.text_ja).")

    raise ValueError(f"TTS_PROVIDER inconnu : {provider}")


def build_episode_voices(
    script_path: Path,
    voices_dir: Path,
    dry_run: bool = False,
    provider_override: str | None = None,
    force: bool = False,
) -> dict[str, VoiceDurationEntry]:
    episode = EpisodeScript.model_validate_json(script_path.read_text(encoding="utf-8"))
    voices_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, VoiceDurationEntry] = {}

    for scene in episode.scenes:
        for line in scene.dialogue_lines:
            if not (line.text.strip() or line.text_ja.strip()):
                continue
            wav_path = voices_dir / f"{line.line_id}.wav"
            _generate_line_audio(
                line,
                wav_path,
                dry_run=dry_run,
                provider_override=provider_override,
                force=force,
            )
            dur, states = compute_mouth_states_12fps(wav_path, fps=LIPSYNC_FPS)
            manifest[line.line_id] = VoiceDurationEntry(
                line_id=line.line_id,
                character_id=line.character_id,
                wav_path=wav_path.as_posix(),
                duration_sec=dur,
                fps=LIPSYNC_FPS,
                mouth_states_12fps=states,
            )

    manifest_path = voices_dir / "voice_durations.json"
    manifest_path.write_text(
        json.dumps(
            {k: v.model_dump() for k, v in manifest.items()},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Génère les voix (VO japonaise / local) et enveloppes lip-sync 12 fps")
    parser.add_argument("--episode", type=str, default="ep01")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--provider", type=str, default=None, choices=["local", "edge_tts", "elevenlabs"])
    parser.add_argument("--force", action="store_true", help="Régénère les .wav existants")
    args = parser.parse_args()

    script_path = Path("episodes") / args.episode / "script.json"
    voices_dir = Path("workspace") / args.episode / "voices"
    manifest = build_episode_voices(
        script_path,
        voices_dir,
        dry_run=args.dry_run,
        provider_override=args.provider,
        force=args.force,
    )
    print(f"OK -> {len(manifest)} répliques générées dans {voices_dir}")


if __name__ == "__main__":
    main()
