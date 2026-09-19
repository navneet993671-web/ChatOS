# Run doc — Misantropic

FastAPI backend that also serves the frontend (`static/`) from the same origin.
There is **no** JS build step, no bundler, and no npm scripts — `package.json`
exists only to pin `@anthropic-ai/sdk` and record the upstream remote.

---

## 1. Reproduce the uncommitted artifacts

A fresh checkout of this worktree needs no generated files. Everything below is
either already committed or optional.

### 1a. Environment file

`.env` is gitignored, so copy it from the main checkout if it is missing:

```
cp <main-checkout>/.env ./.env
```

It is **all-commented** — every setting is at its default, so the app boots
without it. It contains **no secret values**; do not add any. `.env.example` is
the tracked reference and documents every supported variable.

Config that matters for a local preview:

| Variable | Default | Effect here |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/app.db` | relative to the repo root — **the server must start with the repo root as its working directory**, or a new empty DB is created elsewhere |
| `AUTH_ENABLED` | `true` | the app requires login, so `/` redirects to `/login` |
| `CHROMADB_HOST` / `CHROMADB_PORT` | `localhost` / `8100` | not running locally; vector memory and RAG log `DEGRADED` and fall back. Non-fatal. |
| `LOCALHOST_BYPASS` | `false` | set `true` only to skip auth for loopback requests while developing |

### 1b. Python dependencies

`requirements.txt` is the source of truth. The app targets Python 3.12 in Docker
(`Dockerfile`), but any 3.10+ interpreter works; the code is 3.10-compatible.

The canonical setup is a project venv:

```
python -m venv venv
venv/Scripts/python -m pip install -r requirements.txt     # Scripts on Windows, bin on POSIX
```

For the current preview this step was **skipped deliberately**: the system
interpreter (`C:\Users\acer\AppData\Local\Programs\Python\Python310\python.exe`)
already satisfies everything `app.py` imports, which was verified with
`python -c "import app"` → `IMPORT OK`. Missing optional extras only downgrade
features (`python-magic` → basic upload sniffing, `croniter` → `Invalid cron
expression` warnings from the task scheduler).

Do **not** run `pip install` against the system interpreter for this project;
use a venv if you need to add packages.

### 1c. Services (all optional)

- **ChromaDB** — `docker compose up -d chromadb`, or point `CHROMADB_HOST`/`PORT`
  at one. Without it the app runs degraded, not broken.
- **Ollama** — expected on the host at `http://127.0.0.1:11434`. Without it the
  UI still renders; `/api/models` returns no models.
- **SearXNG / ntfy** — optional, Docker-only.

### 1d. Data

`data/` holds the live SQLite DB (`app.db`, ~2.1 MB) plus JSON state
(`auth.json`, `sessions.json`, `settings.json`). It is gitignored and is the
user's real data — **do not delete or regenerate it**, and never run
`Base.metadata.create_all` against a stale copy expecting a clean slate.

`auth.json` is read **once at startup** into memory (`AuthManager._load`), so any
edit to a user's `is_admin` or `privileges` needs a server restart to take
effect. There is no API for promoting an existing user — `is_admin` is only
accepted when the account is created (`POST /api/auth/users`).

---

## 2. Run the server

### Port

Use **7001**. That is the documented native-development port (`README.md`,
`misantropic-ui.service`). Port 7000 is reserved for the nginx front end in
Docker, so keeping native on 7001 avoids a collision between the two modes.
Both ports were free on this host when the preview was started.

### Start it

```
python -m uvicorn app:app --host 127.0.0.1 --port 7001
```

Add `--reload` for development. **The working directory must be the repo root**
(see `DATABASE_URL` above).

Cold start takes roughly 30 s (measured ~30–35 s): it loads the FastAPI app, the
FastEmbed ONNX embedding model, and the TTS/STT/MCP subsystems. `curl` will
refuse the connection until then.

### Detached start (Windows / this preview)

`Start-Process` does not resolve shell shims, so name the executable exactly.
`python.exe` is not a shim, but use an absolute path anyway. stdout and stderr
must go to **different** files.

```
powershell -NoProfile -Command "(Start-Process -FilePath 'C:\Users\acer\AppData\Local\Programs\Python\Python310\python.exe' -ArgumentList '-m','uvicorn','app:app','--host','127.0.0.1','--port','7001' -WorkingDirectory 'C:\Users\acer\Downloads\Misantropic' -RedirectStandardOutput '<log>' -RedirectStandardError '<log>.err' -WindowStyle Hidden -PassThru).Id"
```

Note: uvicorn logs to **stderr**, so the `.err` file is the one with content and
the plain log file stays empty. That is expected, not a failure.

Confirm survival and liveness:

```
powershell -NoProfile -Command "Get-Process -Id <pid>"
curl -s http://127.0.0.1:7001/api/health      # {"status":"healthy",...}
```

### Verify

| Endpoint | Expected |
|---|---|
| `GET /api/health` | `200 {"status":"healthy","timestamp":...}` — public |
| `GET /` | `302` → `/login` while unauthenticated |
| `GET /login` | `200`, title `Misantropic — Login` |
| `GET /static/index.html` | `200` |
| `GET /api/models` | `401 Not authenticated` before login (public once authenticated, and it needs Ollama to return models) |

Harmless noise in the log: ChromaDB connection refused (degraded), SMTP/IMAP not
configured, `croniter` missing. The login page also emits two expected `401`s
for `/api/prefs/theme` and `/api/prefs/custom-themes` because it loads
`theme.js` before the user has authenticated.

### Stop it

```
powershell -NoProfile -Command "Stop-Process -Id <pid>"
```
