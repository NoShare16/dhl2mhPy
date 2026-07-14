# dhl2mh

Pipeline: Plenty orders → Shopware enrichment → filter → DHL DeliverIT XML upload
→ label tracking number written back to Plenty. Python rewrite of the original C# project.
Runs as a cron job (one pass per invocation).

## Setup

```bash
python3.12 -m venv .venv          # Python >= 3.12
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # then fill in the actual values
```

Nested settings use double underscores (e.g. `PLENTY__USERNAME`). See
`.env.example` for all available keys and `src/dhl2mh/config.py` for the default values.

## Run

```bash
dhl2mh run                        # full workflow once (DHL UAT, APP_ENV=dev)
dhl2mh run --dry-run              # full run, but NO Plenty write-back and NO report email
APP_ENV=prod dhl2mh run           # uses the DHL production endpoint

# useful flags
dhl2mh run --log-level DEBUG --items-per-page 50 --concurrency 5
```

| `APP_ENV` | DHL endpoint | Sender PartnerId |
|-----------|--------------|------------------|
| `dev` (default) | `deliverit-uat.dhl.com` | `1` |
| `prod` | `deliverit.dhl.com` | `3` |

Plenty and Shopware are **always live** — only the DHL endpoint changes.

> ⚠️ `--dry-run` skips only the Plenty tracking-number write-back and the report email. The **DHL upload still runs**, so when using `APP_ENV=prod` it creates real shipping labels. A true dry run is only safe against the UAT environment.

## Deployment / Updating on the server

The application runs on the server as a Git clone and is executed daily by a cron job. After pushing changes to GitHub, pull them on the server (as root, from the project directory):

```bash
cd /var/www/vhosts/moebel-staude.de/dhl2mh.moebel-staude.de/private/dhl2mh
git pull
.venv/bin/pip install -e .   # only required if dependencies have changed
```

The cron job automatically uses the updated code on its next execution—no changes to the scheduled task are required.

The `.env` file is gitignored and is never modified by `git pull`.

## Manual web trigger (optional)

In addition to the cron job, a small password-protected web interface is available. Pressing the button starts exactly the same process as the cron job (`python -m dhl2mh run`) as a background process.

The results are still delivered via the report email; the web page only shows the current status ("started", "running", or "last result"). The cron job remains completely unaffected.

**Enable it** (in `.env`):

```bash
WEB__USERNAME=yourusername
WEB__PASSWORD=a-long-secure-password
# WEB__SECRET_KEY=  # optional; if empty, derived from the password
```

Empty values disable the web trigger (cron-only operation).

Install the optional web dependencies and test locally:

```bash
.venv/bin/pip install -e ".[web]"
.venv/bin/uvicorn dhl2mh.web:app --host 127.0.0.1 --port 8095
```

**Run as a service (Ubuntu/systemd):**

Adjust `deploy/dhl2mh-web.service` to match your project path and user, then run:

```bash
sudo cp deploy/dhl2mh-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dhl2mh-web
sudo systemctl status dhl2mh-web        # Logs: journalctl -u dhl2mh-web -f
```

The service listens only on `127.0.0.1:8095`.

**In Plesk**, create a subdomain (e.g. `dhl-trigger.moebel-staude.de`), enable Let's Encrypt SSL, and configure a reverse proxy under *Apache & nginx Settings* pointing to:

```text
http://127.0.0.1:8095
```

Example nginx configuration:

```nginx
proxy_pass http://127.0.0.1:8095;
```

The login cookie is configured with the `Secure` flag, so it only works over HTTPS, which matches the Plesk SSL setup.

> ⚠️ When `APP_ENV=prod` is used (as configured in the provided systemd unit), every button click starts a **real** production run: real DHL labels are generated and tracking numbers are written back to Plenty.
>
> Concurrent web-triggered runs are prevented, but a manually started web run is **not** synchronized with the cron job. Do **not** trigger a manual run while the cron job may already be running.

## Documentation

- [`docs/code-reference.md`](docs/code-reference.md) — module-by-module code reference.
- [`docs/logik-dokumentation.md`](docs/logik-dokumentation.md) — business logic (filter rules, service resolution, MatchCodes, `former_parent`, Festwasser handling, and whitelist logic).

## Tests

```bash
pytest
```
