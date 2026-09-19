# Misantropic — Phase 0 Audit & Foundation

Date: 2026-09-19
Auditor: Buffy
Requested workspace: `C:\Users\acer\Downloads\Misantropic\Misantropic`
**Actual repository: `C:\Users\acer\odysseus`** (git, branch `main`, HEAD `70a71f6`)

---

## 1. Headline findings

Two facts change the shape of this phase:

1. **The repository is not in the workspace.** The workspace
   (`Downloads\Misantropic\Misantropic`) contains no source at all — only
   `.freebuff/project-id` and a `data/` folder holding a 244 KB SQLite file
   named `odysseus.db`. The real codebase lives at `C:\Users\acer\odysseus`,
   which is **outside** the workspace and therefore read-only to me until you
   say otherwise.
2. **The spec describes an architecture the repository does not have.**
   There is no Vite, no React, no nginx, no `package.json` build, no port
   `7001`, no `backend`/`ollama` compose service, and no `odysseus.db` or
   `misantropic.db`. See §5 for the full mismatch table.

The audit below is of the **real** repository, read-only.

### Scale of the legacy name

| Metric | Value |
|---|---|
| Files containing `odysseus` (case-insensitive) | **146** |
| Total occurrences (source/config/docs, excl. `data/`, `venv/`, `node_modules/`) | **898** |
| Python files needing edits | 0 imports — but ~35 files contain string/header/env references |

---

## 2. Repository architecture (as it actually is)

```
odysseus/
├── app.py                FastAPI application: 44 KB, middleware, routes, static mount
├── core/                 database, auth, middleware, models, sessions, constants
├── routes/               ~20 route modules (auth, model, email, calendar, mcp, shell, …)
├── services/             search, youtube, hwfit, memory
├── src/                  agent loop, llm core, RAG, tool schemas (Python — NOT frontend)
├── mcp_servers/          email_server.py, memory_server.py
├── static/               **the frontend**: index.html + vanilla JS (js/*.js, ~90 files)
├── scripts/              `odysseus` CLI + 24 `odysseus-*` subcommands + completions
├── tests/                pytest suite
├── docs/                 docs/index.html, odysseus.jpg, demo media
├── Dockerfile            python:3.12-slim, uvicorn app:app --port 7000
├── docker-compose.yml    services: odysseus, chromadb, searxng, ntfy
├── odysseus-ui.service   systemd unit (uses port 8000)
├── .env / .env.example   identical files (6116 bytes each)
└── data/                 app.db (2.1 MB), auth.json, sessions.json, settings.json, …
```

**Backend:** FastAPI + uvicorn. **Frontend:** server-rendered static HTML/vanilla
JS — there is no bundler, no JSX, no build step. `app.py:675` serves
`static/index.html` for `/`; `app.py:348` mounts `static/` at `/static`.

### Endpoints the spec asks to verify — both already exist

- `GET /api/health` — `app.py:735`
- `GET /api/models` — `routes/model_routes.py` (with a per-user cache, `:440`)

---

## 3. Occurrence matrix (requested categorisation)

| Category | Count | Detail / examples |
|---|---|---|
| **Branding / docs** | ~130 | `README.md` (35), `static/index.html` (23), `docs/index.html` (17), `static/landing.html` (16), `ACKNOWLEDGMENTS.md` (7), `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, `ROADMAP.md` |
| **Frontend UI strings** | 274 (whole `static/`) | `static/js/slashCommands.js` (35), `static/app.js` (19), `settings.js`, `storage.js`, `presets.js`, `login.html`, page titles |
| **Backend Python strings** | ~160 | `routes/*` (109), `src/*` (43), `core/*` (6), `app.py` (8), `services/*` (2), `mcp_servers/*` (2) |
| **CLI / shell scripts** | 272 | `scripts/odysseus` + 24 subcommands, `scripts/_lib/cli.py`, bash/zsh completions, `start-macos.sh` (12), `build-macos-app.sh` (22) |
| **Tests** | 23 | `tests/test_security_regressions.py`, `test_cookbook_helpers.py`, … |
| **Docker service / container names** | 5 | compose service **`odysseus`**; SearXNG secret sentinel `odysseus-local-searxng-json-2026-05-30` used as a one-time migration trigger in the searxng entrypoint |
| **Configuration / env** | 11 + 11 | `.env`, `.env.example` — **22 distinct `ODYSSEUS_*` variables** (see below) |
| **Database paths** | 0 | **No code references `odysseus.db` or `misantropic.db`.** Path comes from `DATABASE_URL`, default `sqlite:///./data/app.db` (`core/database.py:25`) |
| **Python imports** | **0** | There is no `odysseus` package. No `import odysseus` / `from odysseus` anywhere — **no import rewrites needed** |
| **Frontend package names** | 0 | `package.json` is not a frontend manifest; it only holds a repo URL and `@anthropic-ai/sdk`. No package is named `odysseus` |
| **Runtime dependencies (high risk)** | see below | env var names, HTTP headers, CLI name, systemd unit |

### 3a. `ODYSSEUS_*` environment variables — 22 names, real runtime config

Renaming these **is a breaking change**: every existing `.env`, systemd
`EnvironmentFile`, scheduled task and shell profile would stop taking effect.

```
ODYSSEUS_PORT                  ODYSSEUS_ADMIN_PASSWORD     ODYSSEUS_ADMIN_USER
ODYSSEUS_INTERNAL_TOKEN        ODYSSEUS_INPROCESS_POLLERS  ODYSSEUS_INPROCESS_TASKS
ODYSSEUS_SCRIPT_HOST           ODYSSEUS_MAIL_ATTACHMENTS_DIR
ODYSSEUS_MAIL_ORIGIN           ODYSSEUS_SINGLE_USER        ODYSSEUS_FALLBACK_OWNER
ODYSSEUS_USER_SHELL            ODYSSEUS_USER_PATH          ODYSSEUS_PATH__
ODYSSEUS_DISABLE_MCP           ODYSSEUS_PERSONAL_UPLOAD_MAX_BYTES
ODYSSEUS_PREFLIGHT_EXIT        ODYSSEUS_CMD_EXIT           ODYSSEUS_SUBS_CACHE
ODYSSEUS_SKIP_RUN_HINT         ODYSSEUS_NO_OPEN            ODYSSEUS_DEMO_MAIL_DIR
```

### 3b. Internal HTTP headers — wire protocol, must change atomically

`app.py:79-80`, `:233`:

```
X-Odysseus-Internal-Token
X-Odysseus-Owner
```

These authenticate the app's internal tool/MCP path. If renamed, the producing
and consuming sides must move in the same commit or internal tools break.

### 3c. Other genuinely load-bearing references

| Artifact | Why it is risky to rename |
|---|---|
| `scripts/odysseus` + 24 `odysseus-*` | user-facing CLI name; referenced by completions, docs, and possibly cron/scheduled tasks |
| `odysseus-ui.service` | systemd unit name; renaming requires reinstall + `systemctl daemon-reload` |
| compose service `odysseus` | changes container name and compose DNS alias |
| `odysseus-local-searxng-json-2026-05-30` | sentinel string the searxng entrypoint greps for to decide whether to regenerate secrets — changing it silently forces secret regeneration |

### 3d. Legacy name inside stored data (row-level)

Not code, but real persisted values:

- `data/scheduled_emails.db` → column/string `odysseus_kind`
- `data/uploads/uploads.json` → stored path `odysseus/data`

---

## 4. Database audit

There are **two different databases**, and they are **not** the same file:

| Location | File | Size | md5 |
|---|---|---|---|
| Real repo | `odysseus/data/app.db` | 2,183,168 B | `be9a937cdfc7027d85fc235b5b529d0a` |
| Workspace | `Downloads/.../data/odysseus.db` | 249,856 B | `ac8234e6e287e9a3c0a7e360cd22730a` |

The repo's live database is `data/app.db` — **`DATABASE_URL` defaults to
`sqlite:///./data/app.db`** and no code mentions `odysseus.db` or
`misantropic.db`. The 244 KB `data/odysseus.db` in the workspace is a
*separate, smaller instance* (2 users, 2 sessions) with the same schema
family; its relationship to `app.db` is unknown. **Which of these is the data
that must be preserved is a decision for you** (see §6).

### Earlier action taken — flagging honestly

Before discovering the real repo, I ran `scripts/migrate_db_name.py` in the
workspace, which produced `Downloads/.../data/misantropic.db` as a verified,
non-destructive copy of `data/odysseus.db`. Because the workspace DB now
looks like it may be the *wrong* database, treat that copy as **provisional**.
It is purely additive — `data/odysseus.db` and its `-wal`/`-shm` sidecars are
untouched — and can be deleted or redone once you pick the source of truth.

### Data-loss trap (applies to whichever DB is chosen)

Both databases are SQLite in **WAL** mode (verified: `journal_mode = wal`,
`integrity_check = ok`, 25 tables). Where a `-wal` sidecar is present, **a
plain file copy can silently drop committed data** — the WAL must be folded in
by SQLite (`VACUUM INTO`) rather than by `cp`. Also, a `.db` file must never be
renamed without its `-wal`/`-shm` companions.

---

## 5. Spec vs. reality — the mismatch to resolve before Phase 1

| Spec item | Reality | Verdict |
|---|---|---|
| Frontend on `localhost:5173` (Vite dev server) | No Vite. Static HTML/vanilla JS served by FastAPI | **Inapplicable** |
| `const BASE = "/api"` in the client | Client uses `const API_BASE = window.location.origin` and same-origin `/api/...` fetches | **Does not exist** |
| Vite proxy `/api` → native backend | No Vite config, no proxy needed — the frontend is same-origin | **Inapplicable** |
| Native backend on `7001` | Backend is `7000` (`APP_PORT`, Dockerfile, uvicorn). `7001` appears nowhere. systemd unit uses `8000` | **New port to introduce** |
| Docker browser on `localhost:7000` | compose maps `${APP_BIND}:${APP_PORT:-7000}:7000` — already 7000 | ✅ Matches |
| Docker "frontend container: Nginx" | No nginx anywhere; the app serves its own static files | **Would be new** |
| Docker service `backend` | Compose service is named `odysseus` | **Rename candidate** |
| Docker service `ollama` | No such service — Ollama is expected on the **host**, reached via `host.docker.internal:11434` | **Would be new** |
| Nginx: serve React static files, proxy `/api/` → `backend:7000`, SSE, WebSockets, cookies, long LLM timeouts | No nginx. SSE exists in-app (chat streaming via `static/js/chatStream.js`) | **Would be new** |
| `data/odysseus.db` / `data/misantropic.db` | Actual DB is `data/app.db` | **Filenames differ** |

Port `7001` **is** available on this machine (nothing listening); `7000`,
`5173`, `11434`, `8080`, `8100`, `8091` were not probed for conflicts.

---

## 6. Safe migration strategy

Principle: **copy, never move; verify, then flip a single config knob.**

1. **Branding / docs / UI strings** — safe to change broadly. No behaviour
   depends on them. Do these first, in one reviewable pass.
2. **Python string literals** — safe individually; verify with the test suite
   (`pytest`) after each area.
3. **Env vars, HTTP headers, CLI name, systemd unit, compose service name** —
   **not** safe as a blind replacement. For each name:
   - read the new name first, keep the old as a deprecated alias (e.g. accept
     `ODYSSEUS_PORT` and `MISANTROPIC_PORT`, prefer the new one, log a warning),
   - migrate `.env` / `.env.example` / systemd / completions,
   - drop the alias only in a later release.
4. **Database** — do **not** rename or move the live file. Point
   `DATABASE_URL` at a new path only after `VACUUM INTO` produces a verified
   copy, then keep the original for at least one release cycle.
5. **Never** global search-and-replace. The 146 files span Python, JS, shell,
   YAML, HTML, Markdown, a systemd unit and a systemd unit's install script;
   a blind `sed` would corrupt the searxng sentinel, the internal-token
   header pair, and the CLI's own filename.

Tooling already written: `scripts/migrate_db_name.py`
(`--check` to report, default to copy-and-verify, `--overwrite` to rebuild).
It opens the source read-only, uses `VACUUM INTO`, re-checks integrity, and
compares table lists and row counts before declaring success.

---

## 7. Environment

| Tool | Version |
|---|---|
| Node | v22.23.2 (npm 10.9.8) |
| Python | 3.10.2 — note the app targets **3.12** in Docker; there is a `venv/` in the repo |
| Docker | 29.7.2 |
| Ollama | 0.34.2 |

---

## 8. Decisions taken

| Question | Decision |
|---|---|
| Where to work | Copied the repo out of `C:\Users\acer\odysseus` into this workspace; the original is untouched and still at `HEAD 70a71f6` with a clean tree |
| Architecture fidelity | Rebrand the real architecture. No Vite/React rewrite. nginx added as a reverse proxy; Ollama added as an opt-in compose service |
| Database source of truth | `odysseus/data/app.db` (2.1 MB) — **not** the 244 KB workspace `odysseus.db` |

### What was copied

The whole tree minus `venv/` (388 MB), `__pycache__/` and `node_modules/`.
Verified byte-identical: `git diff` against `HEAD` was empty immediately after
the copy, and `HEAD` matched the source. 508 source files, plus the workspace's
pre-existing `data/` files.

---

## 9. What Phase 0 changed

### 9a. Rebranding applied

| Surface | Change |
|---|---|
| Product name in prose/docs | `Odysseus` → `Misantropic` across README, LICENSE, ACKNOWLEDGMENTS, ROADMAP, SECURITY, CONTRIBUTING |
| UI titles / wordmarks | `static/index.html`, `landing.html`, `login.html`, `manifest.json`, `sw.js`, `style.css`, `docs/index.html` |
| Env variables | `ODYSSEUS_*` → `MISANTROPIC_*` (22 names) |
| CLI | `scripts/odysseus` → `scripts/misantropic`, plus 21 `odysseus-*` subcommands, bash/zsh completions, and the dispatcher's sibling-discovery prefix |
| systemd | `odysseus-ui.service` → `misantropic-ui.service`, migrated to port 7001, plus `install-service.sh` |
| Docker | compose service `odysseus` → `backend`; GPU overlays retargeted; new `frontend` (nginx) and `ollama` services |
| Assets | `docs/odysseus.jpg` → `docs/misantropic.jpg` (+ references in README and `build-macos-app.sh`) |
| Packaging | `package-lock.json` package name `odysseus-ui` → `misantropic-ui` |
| New file | `nginx.conf` — reverse proxy with SSE, WebSocket, cookie passthrough and 3600s timeouts |
| New file | `core/env_compat.py` — legacy `ODYSSEUS_*` alias shim |

### 9b. Deliberately NOT renamed, and why

These were protected on purpose. A blanket replace would have destroyed
persisted user data or broken a wire protocol.

| Identifier | Occurrences | Why it must stay |
|---|---|---|
| `X-Odysseus-Internal-Token`, `X-Odysseus-Owner`, `X-Odysseus-Kind`, `X-Odysseus-Origin`, `X-Odysseus-Ref`, `X-Odysseus-Demo`, `X-Odysseus-Event`, `X-Odysseus-Signature` | 30 | Written into **the user's mail store** as IMAP headers and searched with `SEARCH HEADER X-Odysseus-Kind`. Renaming orphans every previously-queued or sent message. Needs dual-read (`match either header`) before any rename. |
| `"odysseus-ui"` (header *value*) | 11 | The persisted value of `X-Odysseus-Origin`. Same problem as above. |
| `odysseus_kind` | 12 | A **real SQLite column**: `ALTER TABLE scheduled_emails ADD COLUMN odysseus_kind TEXT` in `data/scheduled_emails.db`. Renaming needs `ALTER TABLE ... RENAME COLUMN` plus the existence-check logic. |
| `odysseus-*` localStorage keys and CSS ids | 145 | ~20 keys hold browser-side user state (`odysseus-theme`, `odysseus-last-user`, `odysseus-model-favorites`, …) and several are CSS class names. Renaming silently resets user preferences. Needs a one-time client-side migration that copies old key → new key. |
| `odysseus-local-searxng-json-2026-05-30` | 2 | A sentinel grepped inside the persisted `searxng-data` volume. Changing it makes the entrypoint regenerate the SearXNG secret on every boot. |
| `github.com/pewdiepie-archdaemon/odysseus` | 10 | The **real upstream remote**. No new remote URL was supplied, so inventing one would be wrong. Update these once the Misantropic remote exists. |
| `licenses/llmfit-MIT-LICENSE.txt` | 1 | Third-party legal text. Must never be edited. |
| `MISANTROPIC_AUDIT.md`, `scripts/migrate_db_name.py` | 31 | Documentation of the legacy names, and the migration tool itself. Must keep referring to them. |

### 9c. Backward compatibility

`core/env_compat.py` mirrors any `ODYSSEUS_*` variable onto `MISANTROPIC_*`
when the new name is unset, and is invoked at import time from `app.py`,
`core/database.py` and `scripts/_lib/cli.py`. Verified:

```
ODYSSEUS_ADMIN_PASSWORD=legacy-secret  ->  MISANTROPIC_ADMIN_PASSWORD = legacy-secret
MISANTROPIC_INPROCESS_TASKS=0 set      ->  explicit new value preserved
second call returns []                 ->  idempotent
```

So existing `.env` files, systemd `EnvironmentFile` entries and shell exports
keep working with no migration step.

---

## 10. Verification performed

| Check | Result |
|---|---|
| 234 Python files parsed (AST) | **pass** — no syntax errors |
| All 21 renamed CLI scripts + dispatcher parsed | **pass** |
| `python scripts/misantropic` | **pass** — lists every subcommand under the new name |
| `core/env_compat` alias behaviour | **pass** — see 9c |
| `docker compose config` | **pass** — valid |
| `docker compose --profile ollama config` | **pass** — services: `frontend`, `backend`, `ollama`, `chromadb`, `searxng`, `ntfy` |
| Line-ending integrity | **pass** — CRLF counts identical to source in every sampled file |
| Protected strings still present | **pass** — 30 headers, 12 `odysseus_kind`, 11 `odysseus-ui` values |
| Source repo untouched | **pass** — `HEAD 70a71f6`, `git status --porcelain` empty |

### Not verified — blocked on two prerequisites

1. **Docker runtime.** Docker Desktop's daemon is not running
   (`npipe:////./pipe/dockerDesktopLinuxEngine` not found). `docker compose
   config` validates client-side, but `build`, `up`, and therefore
   `curl localhost:7000/api/health` and SSE-through-nginx could not run.
2. **Native runtime.** The copied tree has no `venv` (deliberately excluded,
   388 MB) and the system interpreter is Python 3.10 while the app targets
   3.12 in Docker. `app.py` was not imported, so `/api/health`, `/api/models`
   and the Ollama connection are **unverified at runtime**. Only static
   checks were possible.

`nginx.conf` syntax is likewise unverified locally — there is no nginx binary
on this host. It is validated implicitly when the `frontend` container starts,
which fails fast on a bad config.

---

## 11. Needs a human decision

1. **A blind spot in my own rename.** Some `Odysseus` strings are references to
the *mythological character*, not the product. The clearest case is the
built-in character preset in `static/js/presets.js`, whose prompt now reads
"You are Misantropic, king of Ithaca …", and terminal prompts like
`user@odysseus:~`. These read as nonsense under the new name. Their `id` was
correctly left as `'odysseus'` (it is the persisted key), so only the display
name and prompt text are affected. Decide whether these should be reverted to
the myth or kept on-brand.
2. **New remote URL.** 10 occurrences of the upstream GitHub URL remain; the
README's clone instructions still point at the old repository.
3. **The four deferred identifier families** in 9b each need a small,
   purpose-built migration rather than a rename:
   * IMAP headers → search for both spellings, write only the new one.
   * `odysseus_kind` → `ALTER TABLE ... RENAME COLUMN` guarded by a
     `PRAGMA table_info` check.
   * localStorage → one-time copy-old-key-to-new-key shim at app boot.
   * searxng sentinel → leave permanently; it is a one-way migration marker.
4. **Database filename.** `data/app.db` is brand-neutral and `DATABASE_URL`
defaults to it, so it was **left untouched** — zero risk to the 2.1 MB of live
data. If you want the `misantropic.db` name from the spec, that is a
`VACUUM INTO` copy plus a one-line `DATABASE_URL` change, both reversible.
   The orphaned 244 KB `data/odysseus.db` in this workspace is still present and
   untouched; it is not the live database.
