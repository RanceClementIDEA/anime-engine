# anime-engine (Voie A — Méridien Zéro)

Moteur modulaire de production d'animation japonaise piloté par Claude (Python 3.11+ / FFmpeg / CPU local).

## Démarrage rapide (Windows PowerShell)

~~~powershell
# 1. Créer et activer un environnement virtuel
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Installer les dépendances
pip install -r requirements.txt

# 3. Vérifier que les tests passent
pytest -q

# 4. Estimer le budget de l'épisode 1 (Méridien Zéro)
python pipeline.py --episode ep01 --estimate-only

# 5. Générer la scène d'ouverture SC01 en mode --dry-run (0 €, nécessite FFmpeg dans le PATH)
python pipeline.py --episode ep01 --scene SC01 --dry-run
~~~

## Note Windows / Version Python
Si `pip install pydantic` échoue sur Python 3.15 (absence de wheel précompilé pour `pydantic-core`), crée ton `.venv` avec **Python 3.12 ou 3.13** (`py -3.12 -m venv .venv`).