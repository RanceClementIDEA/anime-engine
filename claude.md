# CLAUDE.md — Moteur de Production d'Animé Hybride (`anime-engine` / Voie A)

Ce fichier définit les règles impératives d'architecture, de réalisation, de maîtrise budgétaire et de sécurité pour tout agent IA (Claude Code CLI, Projets Claude) opérant dans le dépôt `anime-engine/`.

---

## 1. Mission & Rôle de Claude

Dans ce projet, **tu es le Directeur Artistique, le Réalisateur (*Enshutsu*) et l'Ingénieur Pipeline**, jamais un générateur de pixels bruts.

* **Cible :** Production d'épisodes complets d'animation japonaise de **20 minutes** (200 à 260 plans, format 16:9 1080p) dans une enveloppe de **~11,76 $ par épisode en croisière** (~15,46 $ pour le pilote S01E01, hors doublage ElevenLabs optionnel).
* **Environnement d'exécution :** Python 3.11+ sur **CPU standard uniquement (aucun GPU local requis)** + FFmpeg CLI + Pillow + `rembg` (ONNX CPU) + API pay-as-you-go (fal.ai / Replicate).
* **Méthode :** Voie A (**Hybride 2D local + I2V sélectif**) adossée au **Bank System** (réutilisation maximale des décors maîtres, sprites personnages et segments OP/ED/Eyecatch).

---

## 2. Garde-fous Non Négociables (Hard Rules)

Toute violation d'une des règles ci-dessous est bloquante :

1. **Gouvernance Git strictement locale :**
   * Commandes autorisées : `git status`, `git diff`, `git log`, `git add`, `git commit`, `git branch`, `git checkout`/`git switch`.
   * **STRICTEMENT INTERDIT :** `git push`, `git remote add/set-url`, `git fetch`/`git pull` destructifs, création de PR, ou toute écriture vers un dépôt distant.
   * Ne jamais commiter `.env`, `workspace/`, `output/` ni aucun fichier contenant une clé API.
2. **Double coupe-circuit budgétaire :**
   * Aucun appel API payant (image, I2V ou TTS payant) ne peut être déclenché sans validation préalable du coût estimé contre **`scene.max_budget_usd`** ET **`episode.max_budget_usd`**.
   * Tout dépassement doit lever une erreur explicite (`BudgetExceededError`) avant d'émettre la requête réseau.
3. **Audio I2V toujours désactivé :**
   * Toute requête vers un modèle Image-to-Video doit forcer **`generate_audio=False`** (ou paramètre équivalent du fournisseur). L'audio est géré exclusivement par notre moteur 3 bus en local.
4. **Idempotence absolue par `shot_id` :**
   * Avant toute génération (voix, décor maître, sprite, *start frame*, clip plan, scène montée), vérifier si le fichier cible existe déjà et est non vide.
   * Si le fichier existe, **le réutiliser sans appel API ni recalcul**, sauf si le flag explicite `--force` ou `--force-shot <shot_id>` est passé par l'utilisateur.
5. **Validation à 0 € obligatoire avant dépense :**
   * Toujours valider la structure via `--estimate-only` et le rythme complet via `--dry-run` avant de proposer ou lancer un rendu payant.
6. **Zéro secret en dur :**
   * Lire `IMAGE_API_KEY`, `I2V_API_KEY`, `ELEVENLABS_API_KEY` et `TTS_PROVIDER` exclusivement depuis les variables d'environnement (`.env`).

---

## 3. Architecture du Dépôt

```text
anime-engine/
├── CLAUDE.md                           # Ce document (source de vérité projet)
├── .env                                # Clés API locales (ignoré par Git)
├── dataset/                            # Base scrapée (settei, genga, decors, fx, metadata.jsonl)
├── config/
│   ├── style_bible.json                # ADN visuel (extrait du dataset + calibré)
│   └── series_bible.json               # Personnages, profils vocaux, décors maîtres, BGM
├── bank/                               # Assets réutilisables sur TOUTE LA SAISON
│   ├── opening/op_master.mp4           # 1m30 (stock saison)
│   ├── ending/ed_master.mp4            # 1m30 (stock saison)
│   ├── eyecatch/eyecatch.mp4           # 10s (stock saison)
│   ├── locations/                      # Décors maîtres 2560x1440 (<loc_id>_<lighting>.png)
│   ├── characters/<char_id>/           # settei.png + bustes émotions RGBA + bouches/yeux
│   └── audio_sfx/                      # Bibliothèque locale de bruitages et musiques (BGM)
├── episodes/
│   └── epXX/
│       ├── script.json                 # Scénario global 20 min (10 à 14 scènes + dialogues)
│       └── scenes/
│           └── SC01.json ... SC14.json # Storyboards modulaires (15 à 25 plans / scène)
├── workspace/epXX/                     # Cache idempotent (ignoré par Git)
│   ├── voices/                         # <line_id>.wav + voice_durations.json (RMS 12 fps)
│   ├── frames/                         # <scene_id>_<shot_id>_start.png
│   ├── shots/                          # <scene_id>_<shot_id>.mp4
│   ├── scenes_rendered/                # <scene_id>.mp4 (Étage 1)
│   └── cost_log.jsonl                  # Journal append-only des coûts API réels
├── output/
│   ├── epXX_master.mp4                 # Master broadcast 1080p 24fps (animé 12fps) -14 LUFS
│   └── epXX_subtitles.ass              # Sous-titres synchronisés générés automatiquement
├── src/
│   ├── models.py                       # Contrats Pydantic v2 (source de vérité schémas)
│   ├── analyzer/build_style_bible.py   # Analyse dataset -> style_bible.json
│   ├── audio/voice_engine.py           # TTS local/API + calcul enveloppe RMS 12 fps
│   ├── generators/
│   │   ├── asset_bank.py               # Décors maîtres 2560x1440 + sprites détourés RGBA
│   │   ├── animate_dialogue.py         # Moteur Kuchipaku (lip-sync 3 états + clignement)
│   │   ├── animate_local.py            # static_pan, parallax_2_5d, insert_cut, impact_frame
│   │   └── animate_i2v.py              # Connecteur I2V sélectif (generate_audio=False)
│   └── compositing/master_episode.py   # Rendu 2 étages, chaîne optique 12 fps, mix 3 bus
├── tests/                              # Tests unitaires mockés (0 €)
└── pipeline.py                         # Orchestrateur CLI principal
```

---

## 4. Production Hiérarchique & Règle 85 / 15

### 4.1 Cloisonnement anti-saturation de contexte
Ne rédige **jamais** les 240 plans d'un épisode dans un seul fichier. Respecte strictement la hiérarchie :
1. `config/style_bible.json` & `config/series_bible.json` (niveau Série).
2. `episodes/epXX/script.json` (niveau Épisode : 10 à 14 scènes réparties en `cold_open`, `act_a`, `act_b`, `preview`).
3. `episodes/epXX/scenes/SCxx.json` (niveau Scène : 15 à 25 plans calibrés sur `voice_durations.json`).
   * Lors de la création ou l'édition d'une scène `SCxx.json`, ne charge en contexte que `style_bible.json`, les personnages/décors utilisés dans cette scène, le bloc `SCxx` de `script.json` et ses durées vocales correspondantes.

### 4.2 Répartition budgétaire obligatoire (Règle 85 / 15)
Sur un épisode de 20 minutes :
* **45 % — `dialogue_lipsync` (0 €) :** décor maître `2560x1440` légèrement flouté (`gblur`) + zoom lent + sprite RGBA + respiration subtile + **Lip-Sync 3 états à 12 fps** (`0=closed`, `1=half`, `2=open` piloté par l'enveloppe RMS du `.wav`) + clignement des yeux déterministe (2 frames toutes les 3,5 à 5 s).
* **25 % — `static_pan`, `parallax_2_5d`, `insert_cut`, `impact_frame` (0 €) :** travellings, séparation premier plan / arrière-plan via `rembg`, inserts (yeux, mains, objets, météo), flashs N&B de 0,12 à 0,16 s.
* **15 % — Segments récurrents (0 € amorti) :** OP (1m30), Eyecatch (10s), ED (1m30) lus depuis `bank/`.
* **15 % MAX — `action_i2v` (~0,05 $/s, plafond 150–180 s / épisode) :** réservé aux scènes marquées `is_sakuga_priority: true` (climax, action, gros plans très expressifs).
  * Scène standard (`is_sakuga_priority: false`) : **95 à 100 %** de plans locaux gratuits (max 1 plan `action_i2v` de 2,5 s si indispensable).
  * Scène climax (`is_sakuga_priority: true`) : jusqu'à **45 %** de plans `action_i2v`, durée unitaire bornée entre **2,0 s et 3,5 s**.

### 4.3 Règles de mise en scène anti-« Visual Novel »
* Aligner chaque plan de dialogue sur `audio_duration_sec + pause_after_sec`. Découper toute réplique > 5 s en au moins 2 plans (locuteur + plan de réaction ou insert).
* Imposer un changement d'axe, de valeur de plan ou de mouvement caméra toutes les **3 à 5 secondes** maximum.
* Réutiliser prioritairement les décors de `bank/locations/` (`reuse_master_bg: true`) pour maintenir un **Bank Ratio ≥ 60 %**.
* Toujours injecter `bank/characters/<char_id>/settei.png` en image de référence lors de la génération d'une *start frame* contenant ce personnage.
* Rédiger `scene_prompt` et `motion_prompt` en anglais précis. Sur un plan `action_i2v`, `motion_prompt` décrit uniquement un mouvement physique ou caméra simple (sans répéter le style global).

---

## 5. Contrats Techniques, Performance & Post-Production

### 5.1 Schémas & Validation (`src/models.py`)
* Tous les JSON (`series_bible.json`, `script.json`, `SCxx.json`) doivent passer la validation **Pydantic v2** avant tout rendu.
* Modes de rendu autorisés (`RenderMode`) : `dialogue_lipsync`, `static_pan`, `parallax_2_5d`, `insert_cut`, `impact_frame`, `action_i2v`.
* Mouvements caméra autorisés (`CameraMove`) : `none`, `zoom_in`, `zoom_out`, `pan_left`, `pan_right`, `shake`.

### 5.2 Conventions de nommage et d'idempotence
Utilise systématiquement ces chemins déterministes pour garantir la reprise sans surcoût :
* Décors maîtres : `bank/locations/<location_id>_<lighting>.png` (2560 × 1440 px)
* Fiche personnage : `bank/characters/<char_id>/settei.png`
* Voix : `workspace/<ep_id>/voices/<line_id>.wav` (48 kHz mono)
* Start frame : `workspace/<ep_id>/frames/<scene_id>_<shot_id>_start.png`
* Clip plan : `workspace/<ep_id>/shots/<scene_id>_<shot_id>.mp4`
* Scène montée : `workspace/<ep_id>/scenes_rendered/<scene_id>.mp4`

### 5.3 Rendu à deux étages (Anti-OOM sur CPU)
Ne jamais charger les 200 à 260 plans dans un unique graphe FFmpeg :
* **Étage 1 (par scène) :** concaténer les 15 à 25 plans de `SCxx`, appliquer la chaîne optique unificatrice (`fps=12,fps=24`, étalonnage léger, bloom écran opacité `0.18–0.25`, grain pellicule `5–8`), mixer les **3 bus audio** :
  1. Bus Voix : passe-haut 80 Hz + compression douce.
  2. Bus SFX : positionnement aux timecodes exacts.
  3. Bus BGM : `sidechaincompress` (auto-ducking de -8 dB sous les voix) + fondus de transition.
* **Étage 2 (Master Épisode) :** assembler dans l'ordre broadcast (`Cold Open` -> `OP` -> `Acte A` -> `Eyecatch` -> `Acte B` -> `ED` -> `Preview`), normaliser à **-14 LUFS** (`loudnorm`), exporter `output/<ep_id>_master.mp4` (`libx264`, `crf=18`, `yuv420p`, AAC 48 kHz 192 kbps) et générer `output/<ep_id>_subtitles.ass`.

### 5.4 Suivi financier (`cost_log.jsonl`)
Chaque appel API réussi doit ajouter une ligne JSON dans `workspace/<ep_id>/cost_log.jsonl` :
```json
{"ts": "2026-11-24T14:30:00Z", "episode": "ep01", "scene": "SC08", "shot_id": "S12", "provider": "fal", "type": "action_i2v", "duration_sec": 2.5, "cost_usd": 0.125, "audio_enabled": false}
```

---

## 6. Commandes CLI Standard

```bash
# 1. Extraire la bible de style depuis le dataset scrapé
python src/analyzer/build_style_bible.py --dataset dataset --out config/style_bible.json

# 2. Générer les voix (TTS local 0 € par défaut) et calculer les enveloppes 12 fps
python -m src.audio.voice_engine --episode ep01

# 3. Estimer le budget et vérifier la règle 85/15 (OBLIGATOIRE avant tout rendu payant)
python pipeline.py --episode ep01 --estimate-only

# 4. Produire l'animatique complète 20 min à 0 € (dry-run avec vraies voix)
python pipeline.py --episode ep01 --dry-run

# 5. Rendre une seule scène (itération rapide)
python pipeline.py --episode ep01 --scene SC01

# 6. Compiler l'épisode complet (avec contrôle budgétaire bloquant)
python pipeline.py --episode ep01

# 7. Lancer la suite de tests unitaires (100 % mockée, 0 €)
pytest -q
```

---

## 7. Standards de Code & Definition of Done

* **Python 3.11+** entièrement typé (`from __future__ import annotations`, type hints stricts, `pathlib.Path`).
* **Tolérance aux pannes :** gérer proprement les timeouts réseau, retries avec backoff exponentiel et sorties FFmpeg en erreur (`subprocess.run(..., check=True)` avec capture de `stderr` lisible).
* **Tests obligatoires pour toute modification du pipeline :**
  * Les tests ne doivent **jamais** effectuer d'appels API réels (mocks `httpx` obligatoires).
  * Couvrir systématiquement : la validation Pydantic, le blocage `BudgetExceededError` (niveau scène et niveau épisode), l'idempotence (0 appel si fichier déjà en cache), le forçage `generate_audio=False`, et le timing du moteur Lip-Sync 12 fps.
* **Avant de conclure une tâche de code :**
  1. Exécuter `pytest -q` et vérifier que 100 % des tests passent.
  2. Vérifier `git status` et `git diff` (aucun fichier secret ni fichier lourd `workspace/`/`output/` suivi).
  3. Ne jamais exécuter `git push`.