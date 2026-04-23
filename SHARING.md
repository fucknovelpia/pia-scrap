This copy was sanitized for sharing.

What was removed:
- `.env`
- `.api.json`
- `.venv`
- `output/`
- local cache files and `__pycache__/`
- local absolute launcher path

Before publishing or sharing:
- review `README.md`
- make sure no sample outputs are added back in
- keep `.env`, `.api.json`, `.venv`, and `output/` out of git

Quick start for a new user:
1. Create a virtualenv and install `requirements.txt`
2. Copy `.env.example` to `.env` and fill credentials, or use CLI flags
3. Run `python3 main.py --ui` or `./run-pia-ui.command`
