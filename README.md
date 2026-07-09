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

## Manueller Web-Trigger (optional)

Neben dem Cron gibt es einen kleinen passwortgeschützten Web-Button, der genau
denselben Vorgang startet wie der Cron (`python -m dhl2mh run`, als Hintergrund-
prozess). Das Ergebnis kommt weiterhin per Report-Mail; die Seite zeigt nur
„gestartet / läuft / letztes Ergebnis". Der Cron bleibt davon unberührt.

**Aktivieren** (in `.env`):

```bash
WEB__USERNAME=einbenutzer
WEB__PASSWORD=ein-langes-passwort
# WEB__SECRET_KEY=  # optional; leer => aus dem Passwort abgeleitet
```

Leere Werte = Trigger deaktiviert (reiner Cron-Betrieb). Web-Abhängigkeiten
installieren und lokal testen:

```bash
.venv/bin/pip install -e ".[web]"
.venv/bin/uvicorn dhl2mh.web:app --host 127.0.0.1 --port 8095
```

**Als Dienst (Ubuntu/systemd):** `deploy/dhl2mh-web.service` an den Projektpfad
und den User anpassen, dann:

```bash
sudo cp deploy/dhl2mh-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dhl2mh-web
sudo systemctl status dhl2mh-web        # Logs: journalctl -u dhl2mh-web -f
```

Der Dienst lauscht nur auf `127.0.0.1:8095`. **In Plesk** eine Subdomain
(z. B. `dhl-trigger.moebel-staude.de`) anlegen, dort Let's-Encrypt-SSL
aktivieren und unter *Apache & nginx Settings* eine Reverse-Proxy-Regel auf
`http://127.0.0.1:8095` setzen (nginx: `proxy_pass http://127.0.0.1:8095;`).
Das Login-Cookie ist auf `secure` gesetzt, funktioniert also nur über HTTPS —
was mit dem Plesk-SSL genau passt.

> ⚠️ Mit `APP_ENV=prod` (wie im Unit-File) löst jeder Klick einen **echten**
> Lauf aus: echte DHL-Labels + Plenty-Writeback. Zwei gleichzeitige Läufe im
> Web sind gesperrt; ein Web-Trigger **während** der Cron läuft ist aber nicht
> prozessübergreifend gesperrt — nicht zur Cron-Zeit manuell starten.

## Documentation

- [`docs/code-reference.md`](docs/code-reference.md) — module-by-module code reference.
- [`docs/logik-dokumentation.md`](docs/logik-dokumentation.md) — business logic
  (filter rules, service resolution, MatchCodes, former_parent, Festwasser, whitelist).

## Test

```bash
pytest
```
