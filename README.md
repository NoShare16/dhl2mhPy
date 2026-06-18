# dhl2mh

Pipeline: Plenty orders → Shopware enrichment → filter → DHL DeliverIT XML upload
→ label tracking number back to Plenty. Python rewrite of the original C# project.
Runs as a cronjob (one pass per invocation).

## Setup

```bash
python3.12 -m venv .venv          # Python >= 3.12
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # then fill in real values
```

Nested settings use a double underscore (e.g. `PLENTY__USERNAME`). See
`.env.example` for all keys and `src/dhl2mh/config.py` for defaults.

## Run

```bash
dhl2mh run                        # full workflow once (DHL UAT, APP_ENV=dev)
dhl2mh run --dry-run              # full run, but NO Plenty write-back and NO mail
APP_ENV=prod dhl2mh run           # against DHL production

# useful flags
dhl2mh run --log-level DEBUG --items-per-page 50 --concurrency 5
```

| `APP_ENV` | DHL endpoint | Sender PartnerId |
|-----------|--------------|------------------|
| `dev` (default) | `deliverit-uat.dhl.com` | `1` |
| `prod` | `deliverit.dhl.com` | `3` |

Plenty and Shopware are **always live** — only the DHL endpoint switches.

> ⚠️ `--dry-run` skips only the Plenty write-back and the report mail. The **DHL
> upload still runs**, so against `APP_ENV=prod` it creates real labels. A true
> dry run is only safe in UAT.

## Deployment / Update on the server

The code runs on the server as a Git clone, executed by a daily cron job. After
pushing to GitHub, pull the changes on the server (as root, in the project dir):

```bash
cd /var/www/vhosts/moebel-staude.de/dhl2mh.moebel-staude.de/private/dhl2mh
git pull
.venv/bin/pip install -e .   # only needed when dependencies changed
```

The cron job picks up the new code on its next run — no change to the scheduled
task needed. The `.env` is gitignored and is never touched by `git pull`.

## Documentation

- [`docs/code-reference.md`](docs/code-reference.md) — module-by-module code reference.
- [`docs/logik-dokumentation.md`](docs/logik-dokumentation.md) — business logic
  (filter rules, service resolution, MatchCodes, former_parent, Festwasser, whitelist).

## Test

```bash
pytest
```
