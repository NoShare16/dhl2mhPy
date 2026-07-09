"""Optional manual-trigger web UI.

A tiny password-protected page with a single button that runs the *very same*
command as the cron job (``python -m dhl2mh run``) as a background subprocess.
Behaviour is therefore identical to the cron run; the result still arrives by
report mail. The page only reports "started / running / last result".

Enable it by filling ``WEB__USERNAME`` / ``WEB__PASSWORD`` in ``.env`` and
serving this module with uvicorn::

    .venv/bin/uvicorn dhl2mh.web:app --host 127.0.0.1 --port 8095

Then put it behind Plesk (reverse proxy + Let's Encrypt) on its own subdomain.
Bind to 127.0.0.1 so it is only reachable through the Plesk proxy, never the
raw port. A run may take several minutes (DHL label wait); the request returns
immediately and the page polls for status.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from dhl2mh.config import Settings, get_settings

log = structlog.get_logger()

app = FastAPI(title="dhl2mh manueller Trigger", docs_url=None, redoc_url=None)

COOKIE_NAME = "dhl2mh_session"
SESSION_TTL = 8 * 60 * 60  # 8 hours
OUTPUT_TAIL_LINES = 200


# ── run state ────────────────────────────────────────────────────────────────


@dataclass
class RunState:
    """Single-slot state for the one background run we allow at a time."""

    proc: asyncio.subprocess.Process | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    returncode: int | None = None
    output: deque[str] = field(default_factory=lambda: deque(maxlen=OUTPUT_TAIL_LINES))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.returncode is None

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "started_at": self.started_at.isoformat(timespec="seconds")
            if self.started_at
            else None,
            "finished_at": self.finished_at.isoformat(timespec="seconds")
            if self.finished_at
            else None,
            "returncode": self.returncode,
            "output": list(self.output),
        }


_state = RunState()


async def _start_run() -> bool:
    """Launch ``python -m dhl2mh run`` in the background. False if one is live."""
    async with _state.lock:
        if _state.running:
            return False
        _state.started_at = datetime.now()
        _state.finished_at = None
        _state.returncode = None
        _state.output.clear()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "dhl2mh",
            "run",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        _state.proc = proc
    log.info("web.run_started", pid=proc.pid)
    asyncio.create_task(_drain(proc))
    return True


async def _drain(proc: asyncio.subprocess.Process) -> None:
    """Stream the subprocess output into the tail buffer, record the exit code."""
    assert proc.stdout is not None
    async for raw in proc.stdout:
        _state.output.append(raw.decode(errors="replace").rstrip("\n"))
    rc = await proc.wait()
    _state.returncode = rc
    _state.finished_at = datetime.now()
    _state.proc = None
    log.info("web.run_finished", returncode=rc)


# ── auth (HMAC-signed session cookie, no extra dependency) ────────────────────


def _secret(settings: Settings) -> bytes:
    key = settings.web.secret_key or settings.web.password
    return hashlib.sha256(key.encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(username: str, settings: Settings) -> str:
    payload = json.dumps(
        {"u": username, "exp": int(time.time()) + SESSION_TTL},
        separators=(",", ":"),
    ).encode()
    body = _b64(payload)
    sig = _b64(hmac.new(_secret(settings), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _valid_session(token: str | None, settings: Settings) -> bool:
    if not token:
        return False
    try:
        body, sig = token.split(".", 1)
        expected = _b64(
            hmac.new(_secret(settings), body.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(sig, expected):
            return False
        payload = json.loads(base64.urlsafe_b64decode(body + "=="))
        return float(payload.get("exp", 0)) > time.time()
    except Exception:
        return False


def _credentials_ok(username: str, password: str, settings: Settings) -> bool:
    # constant-time on both fields to avoid leaking which one was wrong
    u_ok = hmac.compare_digest(username, settings.web.username)
    p_ok = hmac.compare_digest(password, settings.web.password)
    return u_ok and p_ok


def _is_authed(request: Request, settings: Settings) -> bool:
    return _valid_session(request.cookies.get(COOKIE_NAME), settings)


# ── routes ───────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE


@app.get("/status")
async def status(request: Request) -> JSONResponse:
    settings = get_settings()
    if not settings.web.enabled:
        return JSONResponse({"authed": False, "disabled": True})
    if not _is_authed(request, settings):
        return JSONResponse({"authed": False})
    return JSONResponse({"authed": True, **_state.as_dict()})


@app.post("/login")
async def login(request: Request) -> JSONResponse:
    settings = get_settings()
    if not settings.web.enabled:
        return JSONResponse({"error": "Web-Trigger ist nicht konfiguriert."}, status_code=503)
    data = await request.json()
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))
    if not _credentials_ok(username, password, settings):
        log.warning("web.login_failed", username=username)
        return JSONResponse({"error": "Benutzername oder Passwort falsch."}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE_NAME,
        _sign(username, settings),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=True,  # served via HTTPS (Plesk); harmless to require it
    )
    log.info("web.login_ok", username=username)
    return resp


@app.post("/logout")
async def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.post("/trigger")
async def trigger(request: Request) -> Response:
    settings = get_settings()
    if not _is_authed(request, settings):
        return JSONResponse({"error": "Nicht angemeldet."}, status_code=401)
    started = await _start_run()
    if not started:
        return JSONResponse(
            {"error": "Es läuft bereits ein Durchlauf.", **_state.as_dict()},
            status_code=409,
        )
    return JSONResponse({"ok": True, **_state.as_dict()})


# ── page (single self-contained HTML document, no external assets) ────────────


_PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dhl2mh – Manueller Trigger</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; margin: 0; padding: 2rem 1rem;
         display: flex; justify-content: center; background: Canvas; color: CanvasText; }
  main { width: 100%; max-width: 640px; }
  h1 { font-size: 1.4rem; margin: 0 0 1.5rem; }
  .card { border: 1px solid color-mix(in srgb, CanvasText 20%, transparent);
          border-radius: 12px; padding: 1.5rem; margin-bottom: 1rem; }
  label { display: block; font-size: .85rem; margin: .6rem 0 .2rem; opacity: .8; }
  input { width: 100%; padding: .6rem .7rem; font-size: 1rem; border-radius: 8px;
          border: 1px solid color-mix(in srgb, CanvasText 30%, transparent);
          background: Field; color: FieldText; }
  button { font-size: 1rem; padding: .7rem 1.2rem; border-radius: 8px; border: 0;
           cursor: pointer; background: #c8102e; color: #fff; font-weight: 600; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button.secondary { background: transparent; color: CanvasText;
                     border: 1px solid color-mix(in srgb, CanvasText 30%, transparent); }
  .row { display: flex; align-items: center; gap: .8rem; flex-wrap: wrap; }
  .badge { font-size: .8rem; padding: .2rem .6rem; border-radius: 999px; font-weight: 600; }
  .badge.idle { background: color-mix(in srgb, CanvasText 15%, transparent); }
  .badge.run  { background: #f59e0b; color: #000; }
  .badge.ok   { background: #16a34a; color: #fff; }
  .badge.err  { background: #dc2626; color: #fff; }
  pre { background: color-mix(in srgb, CanvasText 8%, transparent); padding: .8rem;
        border-radius: 8px; overflow-x: auto; font-size: .8rem; max-height: 320px;
        white-space: pre-wrap; word-break: break-word; }
  .muted { opacity: .65; font-size: .85rem; }
  .err-msg { color: #dc2626; font-size: .85rem; min-height: 1.1em; }
  .hidden { display: none; }
</style>
</head>
<body>
<main>
  <h1>DHL-Workflow · manueller Start</h1>

  <section id="login" class="card hidden">
    <p class="muted">Bitte anmelden.</p>
    <label for="u">Benutzer</label>
    <input id="u" autocomplete="username">
    <label for="p">Passwort</label>
    <input id="p" type="password" autocomplete="current-password">
    <p class="err-msg" id="loginErr"></p>
    <button id="loginBtn">Anmelden</button>
  </section>

  <section id="dash" class="card hidden">
    <div class="row" style="justify-content: space-between;">
      <span id="badge" class="badge idle">bereit</span>
      <button class="secondary" id="logoutBtn">Abmelden</button>
    </div>
    <p class="muted" style="margin:.9rem 0;" id="meta">Noch kein Durchlauf in dieser Sitzung.</p>
    <div class="row">
      <button id="runBtn">Jetzt ausführen</button>
      <span class="muted">Läuft denselben Vorgang wie der tägliche Cron. Ergebnis kommt per Mail.</span>
    </div>
    <p class="err-msg" id="runErr"></p>
    <pre id="out" class="hidden"></pre>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
let pollTimer = null;

async function api(path, body) {
  const opt = { method: body ? 'POST' : 'GET', headers: {} };
  if (body) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (_) {}
  return { ok: r.ok, status: r.status, data };
}

function show(state) {
  $('login').classList.toggle('hidden', state !== 'login');
  $('dash').classList.toggle('hidden', state !== 'dash');
}

function render(s) {
  const badge = $('badge');
  if (s.running) {
    badge.className = 'badge run'; badge.textContent = 'läuft …';
    $('runBtn').disabled = true;
  } else {
    $('runBtn').disabled = false;
    if (s.returncode === 0) { badge.className = 'badge ok'; badge.textContent = 'letzter Lauf: OK'; }
    else if (s.returncode !== null) { badge.className = 'badge err'; badge.textContent = 'letzter Lauf: Fehler (' + s.returncode + ')'; }
    else { badge.className = 'badge idle'; badge.textContent = 'bereit'; }
  }
  if (s.started_at) {
    let m = 'Gestartet: ' + s.started_at.replace('T', ' ');
    if (s.finished_at) m += ' · Beendet: ' + s.finished_at.replace('T', ' ');
    $('meta').textContent = m;
  }
  const out = $('out');
  if (s.output && s.output.length) {
    out.classList.remove('hidden');
    out.textContent = s.output.join('\\n');
  }
  // poll while running
  if (s.running && !pollTimer) pollTimer = setInterval(refresh, 2000);
  if (!s.running && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function refresh() {
  const { data } = await api('/status');
  if (data.disabled) { show('login'); $('loginErr').textContent = 'Web-Trigger ist nicht konfiguriert.'; return; }
  if (!data.authed) { show('login'); return; }
  show('dash'); render(data);
}

$('loginBtn').onclick = async () => {
  $('loginErr').textContent = '';
  const { ok, data } = await api('/login', { username: $('u').value, password: $('p').value });
  if (!ok) { $('loginErr').textContent = data.error || 'Anmeldung fehlgeschlagen.'; return; }
  await refresh();
};

$('logoutBtn').onclick = async () => { await api('/logout', {}); show('login'); };

$('runBtn').onclick = async () => {
  $('runErr').textContent = '';
  const { ok, data } = await api('/trigger', {});
  if (!ok) { $('runErr').textContent = data.error || 'Start fehlgeschlagen.'; }
  if (data) render(data);
  refresh();
};

$('p').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('loginBtn').click(); });
refresh();
</script>
</body>
</html>"""