<div align="center">

# nmdl-bot

**A Telegram bot that drives [N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE) for interactive Widevine / HLS / DASH downloads.**

Built on Pyrogram + TgCrypto. Paginated track picker, queued concurrency, live progress, generated thumbnails, owner controls, single-file Docker deployment.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Pyrogram 2.x](https://img.shields.io/badge/pyrogram-2.x-2CA5E0.svg)](https://docs.pyrogram.org/)
[![Docker ready](https://img.shields.io/badge/docker-ready-2496ED.svg)](#run-with-docker)
[![License](https://img.shields.io/badge/license-see%20LICENCE-lightgrey.svg)](licence)

</div>

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
  - [Run with Docker](#run-with-docker)
  - [Run from source](#run-from-source)
- [Configuration](#configuration)
- [Command reference](#command-reference)
- [Usage example](#usage-example)
- [Architecture notes](#architecture-notes)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Security notes](#security-notes)
- [Licence](#licence)

---

## Features

- **Conversational `/dl` flow** — paste a raw `N_m3u8DL-RE` command, the bot parses URL, headers, and Widevine keys via `shlex` and walks you through the picks.
- **Paginated track picker** — `« Prev | n / N | Next »` for manifests with many qualities, languages, or subtitle tracks. Page size is configurable.
- **Custom file names** — name the upload yourself, or `/skip` for the auto-generated default. The bot sanitises and renames the file before sending.
- **Concurrency control** — global `asyncio.Semaphore`; queued users see their position in real time.
- **Live progress** — parses N_m3u8DL-RE's stdout (handles `\r` redraws + ANSI), updates the status message at most once per N seconds, swallowing `FloodWait` and `MessageNotModified`.
- **Pro-looking uploads** — `ffmpeg` grabs a thumbnail from the 10 s mark, scaled to 320 px, attached as `thumb=` on `send_document`.
- **Owner toolbox** — persistent allow-list, restart, host shell, log tailing, all gated by your numeric Telegram id.
- **Hands-off cleanup** — every job's working directory is wiped in a `try/finally`, on success, error, or `/cancel`.
- **One-shot Docker image** — N_m3u8DL-RE, Bento4 `mp4decrypt`, and `ffmpeg` baked in.

## How it works

```
                     ┌──────────────┐
   /dl … URL/key/-H  │ shlex parser │
   ────────────────► │  url+headers │
                     │   + keys     │
                     └──────┬───────┘
                            │ stdin closed, idle-timeout
                     ┌──────▼─────────────────────────┐
                     │ N_m3u8DL-RE --more-info        │
                     │ (probe, parse Spectre table)   │
                     └──────┬─────────────────────────┘
                            ▼
              Inline keyboards (paginated):
              video ─► audio ─► subtitles ─► filename
                            │
                            ▼  asyncio.Semaphore(MAX_CONCURRENT)
                     ┌──────▼─────────────────────────┐
                     │ N_m3u8DL-RE  + mp4decrypt      │
                     │  ─► live progress on stdout    │
                     │  ─► mux to .mkv                │
                     └──────┬─────────────────────────┘
                            ▼
                     ┌──────────────┐
                     │ ffmpeg thumb │ at 00:00:10, 320 px
                     └──────┬───────┘
                            ▼
                     send_document(thumb=…) ─► user
                            │
                            ▼
                     try/finally ─► wipe work_dir
```

## Requirements

| | Why |
|---|---|
| **Python 3.10+** | uses PEP 604 unions and `match`-friendly typing |
| **Pyrogram 2.x + TgCrypto** | Telegram MTProto client + native AES |
| **N_m3u8DL-RE** | actual downloader / decrypter (`v0.3.0-beta`+) |
| **mp4decrypt** (Bento4) | Widevine PSSH/key decryption helper |
| **ffmpeg** | thumbnail generation, optional muxing fallback |

The Docker image installs all native binaries for you.

## Quick start

### Run with Docker

The included [`Dockerfile`](Dockerfile) bundles `N_m3u8DL-RE v0.3.0-beta`, Bento4 `mp4decrypt 1-6-0-641`, and `ffmpeg`.

```bash
git clone https://github.com/cornsnaker/Nmdl.git
cd Nmdl

docker build -t nmdl-bot .

docker run -d --name nmdl-bot --restart unless-stopped \
  -e API_ID=12345 \
  -e API_HASH=0123456789abcdef0123456789abcdef \
  -e BOT_TOKEN=123456:ABC-DEF... \
  -e OWNER_ID=987654321 \
  -v "$(pwd)/downloads:/app/downloads" \
  -v "$(pwd)/session:/app/session" \
  nmdl-bot
```

- `downloads/` is **scratch** — every job's subdir is wiped after upload.
- `session/` persists Pyrogram's bot session across restarts.

### Run from source

```bash
# Install N_m3u8DL-RE, mp4decrypt, and ffmpeg on $PATH first.
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env       # then edit .env with your credentials
set -a; source .env; set +a
python -u bot.py
```

## Configuration

Every option is an environment variable. See [`.env.example`](.env.example) for the canonical list.

### Required

| Variable | Description |
|---|---|
| `API_ID` | from <https://my.telegram.org/apps> |
| `API_HASH` | from <https://my.telegram.org/apps> |
| `BOT_TOKEN` | from [@BotFather](https://t.me/BotFather) |

### Recommended

| Variable | Description |
|---|---|
| `OWNER_ID` | Your numeric Telegram user id. Required to enable owner-only commands. |
| `AUTH_FILE` | Path to the persistent allow-list (default `./auth_users.json`). |

### Optional tuning

| Variable | Default | Description |
|---|---|---|
| `MAX_CONCURRENT` | `2` | Global download semaphore. |
| `PAGE_SIZE` | `6` | Track buttons per inline-keyboard page. |
| `PROGRESS_INTERVAL` | `4.0` | Seconds between status edits during downloads/uploads. |
| `PROBE_TIMEOUT` | `60` | Hard cap (s) on the manifest probe. |
| `PROBE_IDLE` | `3.0` | Idle seconds after which the probe is considered done. |
| `DOWNLOAD_TIMEOUT` | `7200` | Hard cap (s) on a single download (default 2 h). |
| `SHELL_TIMEOUT` | `60` | Hard cap on `/shell` command output. |
| `LOG_FILE` | `./bot.log` | Rotating file log target. |
| `LOG_BUFFER` | `1000` | In-memory ring-buffer size for `/logs`. |
| `LOG_LEVEL` | `INFO` | Standard `logging` levels. |
| `SESSION_NAME` | `nmdl_bot` | Pyrogram session name (file path inside Docker). |
| `NMDL_BIN` | `N_m3u8DL-RE` | Override path / binary name. |
| `FFMPEG_BIN` | `ffmpeg` | Override path / binary name. |
| `DOWNLOAD_ROOT` | `./downloads` | Per-job working directories live here. |

## Command reference

### Public (authorized users)

| Command | Description |
|---|---|
| `/start` | Welcome screen. |
| `/help` | Full usage reference. |
| `/dl <args>` | Start a new download. Accepts `URL`, multiple `-H`/`--header`, multiple `--key`. |
| `/skip` | Accept the auto-generated file name when prompted. |
| `/cancel` | Abort your active job(s). |

### Owner only

| Command | Description |
|---|---|
| `/auth <user_id>` | Add a user to the allow-list. |
| `/unauth <user_id>` | Remove a user from the allow-list. |
| `/authlist` | Show all authorized users. |
| `/logs [N]` | Tail the last `N` log lines (default 50). Sent inline if small, as a `.txt` otherwise. |
| `/shell <cmd>` | Run a shell command on the host (capped by `SHELL_TIMEOUT`). |
| `/restart` | Re-exec the bot process. Active subprocesses are terminated first. |

The owner is whoever's numeric Telegram id matches `OWNER_ID`. Unauthorized users get a clear "ask the owner" message containing their id.

## Usage example

```
You:    /dl N_m3u8DL-RE "https://example.com/manifest.mpd" \
        -H "User-Agent: Mozilla/5.0" \
        -H "Cookie: session=…" \
        --key 11112222333344445555666677778888:aabbccddeeff…

Bot:    🔍 Probing manifest…
Bot:    🎬 Pick a video quality
        [ 1080p | avc1.640028 | 5.5 Mbps ]
        [  720p | avc1.64001f | 3.0 Mbps ]
        [  480p | avc1.640015 | 1.2 Mbps ]
        [  « Prev   1/2   Next » ]
        [             ✖ Cancel             ]

You:    (taps 1080p) → audio → subtitles
Bot:    📝 Send a file name (no extension), or /skip
You:    My Awesome Episode S01E03

Bot:    ⏳ Queued… (position 1)
Bot:    ⬇️ Downloading
        [■■■■■■■□□□□□]  58.3%
        📦 1.4 GB / 2.4 GB
        🚀 12.3 MB/s
Bot:    🖼️ Generating thumbnail…
Bot:    ⬆️ Uploading
        [■■■■■■■■■■■■] 100.0%
Bot:    ✅ Done — sends My Awesome Episode S01E03.mkv
```

## Architecture notes

- **Track-table parser.** Walks the Spectre.Console output of `N_m3u8DL-RE --more-info`, splits on `│`, and classifies the first cell as `Vid`/`Aud`/`Sub`. Header rows and ANSI escapes are stripped first.
- **Carriage-return aware reader.** N_m3u8DL-RE redraws progress with `\r`, so the standard `readline()` blocks. `_stream_lines` reads small chunks and yields on either `\r` or `\n`.
- **Throttled status edits.** A `StatusUpdater` class keeps the last text/edit-timestamp and rate-limits `editMessageText` calls to once per `PROGRESS_INTERVAL` seconds, with `MessageNotModified` and `FloodWait` handled.
- **Filename safety.** User-supplied names are filtered against `[\\/:*?"<>|\x00-\x1f]`, stripped of leading/trailing whitespace and dots, and capped at 200 characters. The original extension is preserved.
- **Restart strategy.** `os.execv(sys.executable, sys.argv)` after a 1 s flush window. In-flight subprocesses are terminated first so they don't outlive the bot.
- **Auth persistence.** A small JSON file (`AUTH_FILE`) holds the allow-list. The bot never serializes anything more sensitive than user ids.

## Troubleshooting

**The probe finds zero tracks.**
The manifest is unreachable, requires extra headers, or N_m3u8DL-RE printed an unexpected table layout. Run the same command in a shell to confirm; if it works there, paste it as-is to `/dl` (the bot strips the binary name automatically).

**Uploads fail at 50 MB.**
That's the public Bot API limit. Run a [self-hosted Bot API server](https://github.com/tdlib/telegram-bot-api) and point Pyrogram at it; the bot itself has no size cap.

**Progress percentage looks jittery.**
It reflects whichever bar (`Vid`/`Aud`/`Sub`) N_m3u8DL-RE last redrew. That's accurate enough for a Telegram status message but isn't a weighted overall ETA.

**`mp4decrypt: command not found`.**
You're running outside Docker without Bento4 installed. The Dockerfile installs it for you; manually, grab `Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip` and drop `bin/mp4decrypt` into `/usr/local/bin/`.

**The bot keeps saying I'm not authorized.**
Ask the owner to send `/auth <your_user_id>`. The bot tells you the exact id to share. Make sure `OWNER_ID` is set to a numeric value (not a username) for the owner.

## Project layout

```
.
├── bot.py            # the bot (single file, ~1.4 k lines, fully async)
├── requirements.txt  # pyrogram + tgcrypto
├── Dockerfile        # python:3.12-slim + N_m3u8DL-RE + mp4decrypt + ffmpeg
├── .env.example      # canonical list of env vars
├── README.md
└── licence
```

## Security notes

- `/shell` and `/restart` are intentional escape hatches for the owner. Never set `OWNER_ID` to an account you don't fully control.
- The bot runs N_m3u8DL-RE inside the same process tree; arguments are always passed via `argv` (never a shell string), so user-supplied URLs and headers can't break out into shell commands.
- Widevine keys are passed to N_m3u8DL-RE via `--key KID:KEY` argv (also not shell-interpolated). They appear in process arguments — keep `ps`/`top` access restricted on shared hosts.
- Pyrogram's session file (`session/`) is sensitive. Treat it like the bot token itself.

## Licence

See [`licence`](licence) in the repository root.
