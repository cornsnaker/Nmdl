# nmdl-bot

A Telegram bot that wraps [`N_m3u8DL-RE`](https://github.com/nilaoda/N_m3u8DL-RE) for interactive Widevine / HLS / DASH downloads. Built on Pyrogram + TgCrypto for fast MTProto uploads.

## What it does

1. You send `/dl N_m3u8DL-RE "URL" -H "Header: ..." --key KID:KEY` (any number of `-H` and `--key` flags).
2. The bot probes the manifest with `--more-info` and lists every video / audio / subtitle track via paginated inline keyboards (Prev / page indicator / Next).
3. After the three picks you supply a custom file name (or `/skip` to keep the default).
4. The job goes through a global semaphore (default 2 concurrent); waiting users see a queue position.
5. `N_m3u8DL-RE` downloads, decrypts (via `mp4decrypt`), and muxes to MKV.
6. `ffmpeg` grabs a thumbnail at the 10 s mark; the file is uploaded as a document with that thumbnail attached.
7. The per-job working directory is wiped no matter what — success, error, or `/cancel`.

Live progress is parsed from `N_m3u8DL-RE`'s stdout (handles ANSI + `\r` redraws) and the status message is edited at most once per `PROGRESS_INTERVAL` seconds, with `FloodWait` and `MessageNotModified` swallowed.

## Commands

### Public (authorized users)

| Command | Description |
|---|---|
| `/dl …`   | Start a new download (see syntax above) |
| `/skip`   | Keep the auto-generated file name when prompted |
| `/cancel` | Abort your active job(s) |
| `/help`   | Show usage |

### Owner only

| Command | Description |
|---|---|
| `/auth <user_id>`   | Grant access |
| `/unauth <user_id>` | Revoke access |
| `/authlist`         | List authorized user ids |
| `/logs [N]`         | Last `N` log lines (default 50) — sent inline or as a file |
| `/shell <cmd>`      | Run a shell command on the host (60 s default cap) |
| `/restart`          | Re-exec the bot process |

The owner is whoever's numeric Telegram id matches `OWNER_ID`.

## Configuration

Every option is an environment variable. See [`.env.example`](.env.example) for the full list.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `API_ID`, `API_HASH`, `BOT_TOKEN` | yes | — | from <https://my.telegram.org/apps> + BotFather |
| `OWNER_ID` | recommended | `0` | enables owner commands when matched |
| `AUTH_FILE` | no | `./auth_users.json` | persistent allow-list |
| `MAX_CONCURRENT` | no | `2` | global download semaphore |
| `PAGE_SIZE` | no | `6` | track buttons per page |
| `PROGRESS_INTERVAL` | no | `4.0` | seconds between status edits |
| `DOWNLOAD_TIMEOUT` | no | `7200` | seconds (2 h) |
| `SHELL_TIMEOUT` | no | `60` | `/shell` cap |
| `LOG_FILE`, `LOG_BUFFER` | no | `./bot.log`, `1000` | rotating file + in-memory ring |
| `NMDL_BIN`, `FFMPEG_BIN` | no | `N_m3u8DL-RE`, `ffmpeg` | binary names / paths |

## Run with Docker (recommended)

The included `Dockerfile` bakes in:

- `N_m3u8DL-RE v0.3.0-beta` (Linux x64)
- `mp4decrypt` from Bento4 SDK 1-6-0-641
- `ffmpeg` from Debian
- The bot itself + Pyrogram + TgCrypto

```bash
docker build -t nmdl-bot .

docker run --rm -it \
  -e API_ID=12345 \
  -e API_HASH=0123456789abcdef0123456789abcdef \
  -e BOT_TOKEN=123456:ABC-DEF... \
  -e OWNER_ID=987654321 \
  -v $(pwd)/downloads:/app/downloads \
  -v $(pwd)/session:/app/session \
  nmdl-bot
```

The `downloads` volume is purely scratch (each job's subdir is wiped after upload). The `session` volume persists Pyrogram's bot session across restarts.

## Run manually

```bash
# Requires N_m3u8DL-RE, mp4decrypt, ffmpeg on $PATH
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit values
set -a; source .env; set +a
python -u bot.py
```

## Notes & limitations

- Default Telegram Bot API caps uploads at **50 MB**. For larger files run a self-hosted Bot API server and point Pyrogram at it.
- The track-table parser assumes Spectre.Console box-drawing (`│`); ASCII-only output would need a tweak.
- Progress percentage reflects whichever bar `N_m3u8DL-RE` last redrew (video / audio / sub), not a weighted overall — accurate enough for UX, not for ETAs.
- `/shell` and `/restart` are intentional escape hatches for the owner; never set `OWNER_ID` to an account you don't fully control.
