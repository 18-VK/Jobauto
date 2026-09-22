# AGENTS.md

This repo contains `jobauto`, a preference-driven job application assistant for Indian job portals (Naukri, LinkedIn, Indeed, Instahyre, Hirist).

## Project purpose

The app discovers jobs, scores them against saved preferences, prepares applications, and then stops for a human to review and submit. The product intentionally keeps a human in the loop.

For the broad project overview and setup steps, start with [README.md](README.md). For deployment and hosting details, see [docs/DEPLOY.md](docs/DEPLOY.md), [docs/HOSTING.md](docs/HOSTING.md), and [docs/RISKS.md](docs/RISKS.md).

## Agent working rules

Keep changes aligned with the repo's product contract and do not broaden scope for convenience:

- Follow the human-in-the-loop design: no silent auto-submit, no password storage, no headless login flow, and no bot-detection evasion.
- Prefer configuration over code when adjusting portal behavior: selectors and portal rules live under [config/](config/), not in Python modules.
- Keep policy in the pipeline and browser mechanics in adapters. If a change can be expressed as a config tweak or a small portal YAML change, prefer that over a broad Python refactor.
- Treat the cloud and local agent as intentionally separate concerns. Cloud state should not hold portal cookies, browser profiles, or local auth state.
- Validate with the smallest relevant pytest target first, then expand to the broader suite only if the change affects shared flow or config validation.

## Core workflow

The canonical flow is:

discover -> score -> prepare -> review -> submit

The orchestration lives in [src/jobauto/pipeline.py](src/jobauto/pipeline.py). Adapters in [src/jobauto/portals/](src/jobauto/portals/) are intentionally dumb; the pipeline owns policy, caps, dedupe, and safety decisions.

## Safety invariants that must be preserved

These are product rules, not optional preferences:

- No application is ever auto-submitted without a human keystroke. This is enforced by the review gate and by portal-level safeguards.
- Never add credential storage or a headless login flow. Browser cookies are kept in the local browser profile, not in app state.
- Portal selectors live in YAML under [config/portals/](config/portals/), not hard-coded in Python.
- A portal redesign should usually be a YAML change, not a Python rewrite.
- Do not bypass manual-review checks or insert CAPTCHA-solving or bot-detection evasion logic.

## Standard commands

```bash
pip install -r requirements.txt
python -m playwright install chromium

python -m pytest -q
python -m pytest tests/test_scoring.py -q

PYTHONPATH=src python -m jobauto doctor
PYTHONPATH=src python -m jobauto login
PYTHONPATH=src python -m jobauto discover
PYTHONPATH=src python -m jobauto shortlist --why
PYTHONPATH=src python -m jobauto apply --limit 5
PYTHONPATH=src python -m jobauto review
python -m jobauto web
```

`pytest.ini` sets `pythonpath = src`, so tests are intended to run from the repo root without an editable install.

## Architecture and conventions

- Config and validation: [config/](config/) holds preferences and portal definitions. Validation failures should surface loudly during config load.
- Scoring: [src/jobauto/scoring.py](src/jobauto/scoring.py) derives ranking from preferences and weights. Prefer config-driven additions rather than hard-coded role logic.
- Forms and screening: [src/jobauto/forms.py](src/jobauto/forms.py) matches live questions against saved profile answers. Uncertain or sensitive answers should escalate instead of being guessed.
- Web dashboard: [src/jobauto/web/](src/jobauto/web/) is a small Flask app with vanilla JS and no build step. Keep it lightweight.
- Cloud split: [src/jobauto/cloud/](src/jobauto/cloud/) and [src/jobauto/agent/](src/jobauto/agent/) are intentionally separated; cloud holds only hosted app state, not portal cookies or browser profiles.

## When making changes

- Prefer YAML-driven config changes over Python hard-coding when the change affects portal selectors or site behavior.
- If a feature affects the application safety or review gate, add or update tests under [tests/](tests/).
- Keep code changes minimal and aligned with the current architecture: pipeline for policy, adapters for scraping and form interaction.
- Match the repo’s existing conventions, especially around explicit validation and conservative behavior.

## Useful references

- [README.md](README.md)
- [docs/DEPLOY.md](docs/DEPLOY.md)
- [docs/HOSTING.md](docs/HOSTING.md)
- [docs/RISKS.md](docs/RISKS.md)
- [resumes/README.md](resumes/README.md)
- [src/jobauto/portals/registry.py](src/jobauto/portals/registry.py)
- [src/jobauto/config.py](src/jobauto/config.py)
- [src/jobauto/review.py](src/jobauto/review.py)

For repo-specific questions, use the project docs above before inventing a new convention.
