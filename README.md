# OptigoApps LLM Chatbot

FastAPI service that answers ERP report questions by routing them to existing Node/Express report endpoints.

## Setup

```bash
# create venv
python -m venv .venv

# activate (Windows)
.venv\Scripts\activate
# activate (Linux/Mac)
source .venv/bin/activate

# install deps
pip install -r requirements.txt

# configure env
cp .env.example .env   # fill in API keys + Node API details
```

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

## Test

```bash
# unit tests
python -m unittest discover -s tests -v

# validate report configs
python scripts/validate_report_configs.py
```

## Export SQLite data to CSV

```bash
# all project DBs -> exports/
python scripts/export_db.py

# custom output / specific DB
python scripts/export_db.py --out mydump
python scripts/export_db.py --db logs/conversations.db
```

## Docs

See `PROJECT.md` for architecture, endpoints, and report-config details.
