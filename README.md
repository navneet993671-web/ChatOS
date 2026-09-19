<<<<<<< HEAD
# Misantropic
───────────────────────────────────────────────
 ⊹ ࣪ ˖ ૮( ˶ᵔ ᵕ ᵔ˶ )っ  Misantropic vers. 1.0
───────────────────────────────────────────────

![Misantropic](Screenshot 2026-09-19 142007.jpg)

A self-hosted AI workspace -- meant to be the self-hosted version of the UI experience you get from ChatGPT and Claude. But with more jank and fun. Running on your own hardwa<img width="1920" height="1080" alt="Screenshot 2026-09-19 142007" src="https://github.com/user-attachments/assets/a1f2b973-fce7-418e-9d41-4b93d23c4869" />
re, with your own data -- local-first, privacy-first, and no trojan.

## Features
  - **Chat** -- chat with any local model or API; adding them is super simple.<br>　<sub>vLLM · llama.cpp · Ollama · OpenRouter · OpenAI</sub>
  - **Agent** -- hand it tools and let it run the whole task itself.<br>　<sub>built on [opencode](https://github.com/anomalyco/opencode) · MCP · web · files · shell · skills · memory</sub>
  - **Cookbook** -- Scans your hardware, recommends models, click to download and serve.. easy!<br>　<sub>built on [llmfit](https://github.com/AlexsJones/llmfit) · VRAM-aware · GGUF / FP8 / AWQ · fit scoring · vLLM / llama.cpp serving</sub>
  - **Deep Research** -- multi-step runs that gather, read, and synthesize sources into a nice visual report.<br>　<sub>adapted from [Tongyi DeepResearch](https://github.com/Alibaba-NLP/DeepResearch)</sub>
  - **Compare** -- a fun tool to compare models side by side. Test completely blind, no bias!<br>　<sub>multi-model · blind test · synthesis</sub>
  - **Documents** -- YOU write the text, AI is there to assist, not the opposite.<br>　<sub>multi-tab editor · markdown · HTML · CSV · syntax highlighting · AI edits · suggestions</sub>
  - **Memory / Skills** -- Persistent memory and skills, your agent evolves over time as it better understands you and your tasks!<br>　<sub>ChromaDB · fastembed (ONNX) · vector + keyword retrieval · import/export</sub>
  - **Email** -- IMAP/SMTP inbox with AI triage built in: urgency reminders, auto-tag, auto-summary, auto-reply drafts, auto-spam.<br>　<sub>IMAP · SMTP · per-account routing · CalDAV-aware</sub>
  - **Notes & Tasks** -- Quick notes with reminders, a todo list, and scheduled tasks the agent can act on.<br>　<sub>note pings · checklist · cron-style tasks · ntfy / browser / email channels</sub>
  - **Calendar** -- Local-first calendar with CalDAV sync to Radicale / Nextcloud / Apple / Fastmail.<br>　<sub>CalDAV pull · .ics import/export · per-calendar colors · agent-aware</sub>
  - **Works on mobile** -- looks and runs great on your phone, not just desktop.<br>　<sub>responsive · installable (PWA) · touch gestures</sub>
  - **Extras** -- more to explore, happy if you give it a go!<br>　<sub>image editor · theme editor · file uploads (vision + PDF) · web search · presets · sessions · 2FA</sub>

## Demo
A full, hover-to-play tour lives on the landing page (`docs/index.html`).

<details>
<summary>Screenshots / clips</summary>

### Chat & Agents
![Chat & Agents](docs/chat.gif)
### Deep Research
![Deep Research](docs/research.gif)
### Compare
![Compare](docs/compare.gif)
### Documents
![Documents](docs/document.gif)
### Notes & Tasks
![Notes & Tasks](docs/notes.gif)

</details>

## Quick Start

Defaults work out of the box: clone, run, then configure models/search/email
inside **Settings**. Only edit `.env` for deployment-level overrides like
`APP_BIND`, `APP_PORT`, `AUTH_ENABLED`, `DATABASE_URL`, or a pre-seeded admin password.

On first setup, Misantropic creates an admin account (`admin` unless
`MISANTROPIC_ADMIN_USER` is set) and prints a temporary password in the terminal.
For Docker installs, the same line is in `docker compose logs backend`.
Use that for the first login, then change it in **Settings**.

Contributing? See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, and
pull request guidelines.


### Installing ffmpeg

Only needed for the server path:

```bash
# Docker — already installed by the Dockerfile, nothing to do

# Windows
winget install Gyan.FFmpeg

# macOS
brew install ffmpeg

# Debian / Ubuntu
sudo apt install ffmpeg
```

If it is not on `PATH`, point at it explicitly. A value that names a missing
file is ignored rather than disabling a working install:

```bash
MISANTROPIC_FFMPEG=/opt/ffmpeg/bin/ffmpeg
MISANTROPIC_FFPROBE=/opt/ffmpeg/bin/ffprobe
```

`GET /api/video/capabilities?refresh=true` re-probes for the binaries, and the
editor does this every time it opens — so installing ffmpeg while the server is
running is picked up on the next open, with no restart.

### Configuration

| Variable | Default | Effect |
|---|---|---|
| `VIDEO_ENABLED` | `true` | Set `false` to disable server-side editing; the browser path still works. |
| `VIDEO_MAX_DURATION_SECONDS` | `3600` | Applies to the **selected range**, not the whole file, so a long recording is fine once trimmed. |
| `VIDEO_MAX_OUTPUT_BYTES` | `2147483648` | An encode past this is deleted and reported as `too_large`. |
| `VIDEO_FFMPEG_TIMEOUT_SECONDS` | `1800` | A hung encode is killed and reported as `timeout`. |
| `VIDEO_TEMP_DIR` | `data/tmp/video` | Scratch space; one directory per job. |
| `VIDEO_KEEP_FAILED_OUTPUTS` | `false` | Keep partial files from failed encodes, for debugging. |

### Ownership, privacy and caching

Video editing follows the gallery's existing rules, because an edit is just
another way to read and write gallery files:

- Every route requires an authenticated session, and a video that is missing
  **or** owned by someone else is a **404** — never a 403, which would confirm
  the id exists.
- A job is visible only to the user who started it. Polling another user's job
  id returns 404.
- Extracted frames become ordinary gallery rows, so they inherit albums, tags,
  ownership and deletion like any other item.
- The encoder's local temp path is stripped from the job response; a
  filesystem path is not something the browser needs to know.

**Why "replace" writes a new file.** Generated media is served with
`Cache-Control: immutable`, because filenames are content hashes. Overwriting a
file's bytes in place would therefore leave browsers showing the *old* video for
a year. A replace updates the row to point at a newly-named file and only then
deletes the old one — so a failed replace can never leave you with neither, and
a successful one is never cached stale.

Temporary audio and video never persist: each job's scratch directory is
deleted as soon as its result is collected, and anything orphaned by a crash is
swept on the next start.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Badge reads "Browser encoder" | No ffmpeg on the server. Install it, or accept the real-time WebM path. |
| Badge reads "Unavailable" | Neither ffmpeg nor a browser encoder. Use a Chromium/Firefox build with `MediaRecorder`. |
| MP4 or GIF cannot be selected | Without ffmpeg only WebM is possible; the option list reflects that. |
| "Replace the original" greyed out | Browser exports cannot replace in place, for the caching reason above. Install ffmpeg. |
| Export is slow | Expected without ffmpeg — it encodes in real time. The progress text shows an estimate. |
| `too_long` (413) | The selected range exceeds `VIDEO_MAX_DURATION_SECONDS`. Trim it further or raise the limit. |
| `too_large` (413) | The encoded file exceeds `VIDEO_MAX_OUTPUT_BYTES`. |
| `timeout` (504) | The encode exceeded `VIDEO_FFMPEG_TIMEOUT_SECONDS`; it was killed, not left running. |
| `ffmpeg_unavailable` (503) | ffmpeg is not on `PATH`; set `MISANTROPIC_FFMPEG`. |
| `Could not read this video file` (422) | ffprobe could not parse the container. The file may be corrupt or truncated. |
| No rotate buttons on a video | Intentional — those call a PIL endpoint that cannot read a video. Use the editor. |
| A video shows the old content after replacing | Hard-refresh once. Media is cached `immutable`; the row now points at a new filename, so a normal reload is enough. |

## Security Notes
Misantropic is a self-hosted workspace with powerful local tools: shell access, file uploads, model downloads, web research, email/calendar integrations, and API tokens. Treat it like an admin console.

- Keep `AUTH_ENABLED=true` for any network-accessible deployment.
- Do not expose it directly to the public internet without HTTPS and a trusted reverse proxy.
- Keep `data/`, `.env`, logs, databases, and uploaded/generated media out of Git. They are ignored by default.
- Review `data/auth.json` after first boot: disable open signup unless you intentionally want it, make only your own account admin, and keep demo/test accounts non-admin.
- Non-admin users do not get shell/Python/file read/write by default, and admin-only routes/tools such as MCP management, API tokens, webhooks, model/cookbook serving, backup/vault, and app settings are admin-gated. Other features are controlled by per-user privileges, so review each user's privileges before exposing a deployment.
- Rotate any API keys or tokens that were ever pasted into a shared chat, demo, screenshot, or log.
- If you enable API tokens or webhooks, create separate tokens per integration and delete unused ones.
- Prefer binding manual development runs to `127.0.0.1`; bind to `0.0.0.0` only when you intentionally want LAN/reverse-proxy access.
- Before publishing a fork, run `git status --short` and confirm no private files from `.env`, `data/`, `logs/`, uploads, backups, or local databases are staged.

### Putting it behind HTTPS
Misantropic serves plain HTTP on its port. That's fine for `localhost` and trusted LAN/VPN use, but browsers will warn ("Password fields present on an insecure page") and the login + API tokens travel in cleartext. For anything reachable outside your machine — including a Tailscale IP shared with other devices — put a TLS-terminating reverse proxy in front.

Shortest path with [Caddy](https://caddyserver.com/) (auto-renews Let's Encrypt certs):

```caddy
misantropic.example.com {
  reverse_proxy localhost:7000
}
```
## Configuration
Most setup is done inside the app with `/setup` or **Settings**. Use `.env`
for deployment-level defaults and secrets you want present before first boot.
Key settings:

| Variable | Default | Description |

                                |||||
                  |    |    |   |||||||
                 )_)  )_)  )_)   ~|~
                )___))___))___)\  |
               )____)____)_____)\\|
             _____|____|____|_____\\\__
             \                       /
       ~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~
               ~^~  all aboard!  ~^~
       ~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~
```
=======
# Misantropic
>>>>>>> 3cb9429fa44edac662621b7c373bef18e01b234f
