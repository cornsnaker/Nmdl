"""
nmdl-bot — Pyrogram Telegram bot wrapping N_m3u8DL-RE.

Workflow
--------
1.  /dl <raw N_m3u8DL-RE command>            (URL + -H headers + --key KID:KEY)
2.  Bot probes the manifest with `--more-info` and parses the track table.
3.  User picks Video → Audio → Subtitles via inline keyboards.
4.  Job is queued behind an asyncio.Semaphore (MAX_CONCURRENT downloads).
5.  N_m3u8DL-RE downloads + decrypts + muxes to MKV; live progress bar is
    pushed back to Telegram (rate-limited to one edit every PROGRESS_INTERVAL s).
6.  ffmpeg grabs a thumbnail at 00:00:10, scales to 320 px wide.
7.  Final MKV is uploaded as a document with the thumbnail attached.
8.  Whatever happens — success, error, /cancel — the temp dir is wiped.

Env vars (required): API_ID, API_HASH, BOT_TOKEN
Env vars (optional): NMDL_BIN, FFMPEG_BIN, DOWNLOAD_ROOT, MAX_CONCURRENT,
                     PROGRESS_INTERVAL, SESSION_NAME

Dependencies: pyrogram>=2.0, tgcrypto, plus N_m3u8DL-RE and ffmpeg on $PATH.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SESSION_NAME = os.getenv("SESSION_NAME", "nmdl_bot")

NMDL_BIN = os.getenv("NMDL_BIN", "N_m3u8DL-RE")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "./downloads")).resolve()
MAX_CONCURRENT = max(1, int(os.getenv("MAX_CONCURRENT", "2")))
PROGRESS_INTERVAL = float(os.getenv("PROGRESS_INTERVAL", "4.0"))  # seconds
PROBE_TIMEOUT = float(os.getenv("PROBE_TIMEOUT", "60"))           # seconds
PROBE_IDLE = float(os.getenv("PROBE_IDLE", "3.0"))                # seconds
DOWNLOAD_TIMEOUT = float(os.getenv("DOWNLOAD_TIMEOUT", "7200"))   # seconds (2h)

if not (API_ID and API_HASH and BOT_TOKEN):
    raise SystemExit("API_ID, API_HASH and BOT_TOKEN env vars must be set")

DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nmdl-bot")

# ---------------------------------------------------------------------------
# Regex / helpers
# ---------------------------------------------------------------------------

# Strip ANSI escape sequences before any text parsing.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
# Match the most-recent percentage in a Spectre.Console progress line.
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
# "12.34MB/56.78MB" or "12.3 MiB / 100 MiB"
SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*([KMG]i?B)\s*/\s*(\d+(?:\.\d+)?)\s*([KMG]i?B)",
    re.IGNORECASE,
)
# "2.5MB/s" or "2.5 MiB/s"
SPEED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMG]i?B/s)", re.IGNORECASE)


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def progress_bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round((pct / 100.0) * width))
    return "[" + "■" * filled + "□" * (width - filled) + f"] {pct:5.1f}%"


def human_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024.0:
            return f"{f:.1f} {unit}"
        f /= 1024.0
    return f"{f:.1f} PB"


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------

def parse_command(raw: str) -> tuple[str, list[str], list[str]]:
    """Parse the user's free-form command into (url, headers_argv, keys_argv).

    Accepts things like:
        N_m3u8DL-RE "https://..." -H "User-Agent: x" --key KID:KEY --key KID2:KEY2
        "https://..." -H "Cookie: a=b" --header="Origin: https://x"

    Returns:
        url               — first non-flag positional argument
        headers_argv      — flat ["-H", "v", "-H", "v", ...] ready for argv
        keys_argv         — flat ["--key", "kid:key", ...] ready for argv
    """
    if not raw or not raw.strip():
        raise ValueError("Empty command")

    tokens = shlex.split(raw, posix=True)

    # Drop leading binary name if the user pasted the full command.
    if tokens and Path(tokens[0]).name.lower().startswith("n_m3u8dl-re"):
        tokens = tokens[1:]

    url: Optional[str] = None
    headers: list[str] = []
    keys: list[str] = []

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header"):
            if i + 1 >= len(tokens):
                raise ValueError(f"Missing value for {t}")
            headers += ["-H", tokens[i + 1]]
            i += 2
        elif t.startswith("--header="):
            headers += ["-H", t.split("=", 1)[1]]
            i += 1
        elif t == "--key":
            if i + 1 >= len(tokens):
                raise ValueError("Missing value for --key")
            keys += ["--key", tokens[i + 1]]
            i += 2
        elif t.startswith("--key="):
            keys += ["--key", t.split("=", 1)[1]]
            i += 1
        elif t.startswith("-"):
            # Quietly ignore unknown flags — selectors are owned by the bot.
            i += 1
        else:
            if url is None:
                url = t
            i += 1

    if not url:
        raise ValueError("No URL found in command")
    return url, headers, keys


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

@dataclass
class Track:
    idx: int
    track_id: str        # value passed to N_m3u8DL-RE selector (id=...)
    label: str           # human readable, used for inline button text
    kind: str            # "vid" | "aud" | "sub"


@dataclass
class Job:
    job_id: str
    short_id: str        # 8-char id used in callback_data (≤ 64-byte limit)
    user_id: int
    chat_id: int
    status_msg_id: int
    url: str
    headers: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)

    videos: list[Track] = field(default_factory=list)
    audios: list[Track] = field(default_factory=list)
    subs: list[Track] = field(default_factory=list)

    chosen_video: Optional[str] = None
    chosen_audio: Optional[str] = None    # "all" | "<id>"
    chosen_sub: Optional[str] = None      # "all" | "none" | "<id>"

    work_dir: Path = field(default_factory=Path)
    proc: Optional[asyncio.subprocess.Process] = None
    cancelled: bool = False


# Global state
JOBS: dict[str, Job] = {}                     # short_id → Job
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)
ACTIVE = 0
WAITING = 0


# ---------------------------------------------------------------------------
# Probe phase: list all tracks
# ---------------------------------------------------------------------------

async def probe_tracks(
    url: str, headers: list[str], keys: list[str], work_dir: Path
) -> tuple[list[Track], list[Track], list[Track]]:
    """Run N_m3u8DL-RE in info mode, parse the printed track table, return tracks.

    The CLI normally prompts for selection; we close stdin and time-out after
    PROBE_IDLE seconds of silence so the table is captured without starting a
    download. The process is then terminated.
    """
    args = [NMDL_BIN, url, "--more-info", *headers, *keys]
    log.info("probe argv: %s", " ".join(shlex.quote(a) for a in args))

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(work_dir),
    )

    buf = bytearray()
    deadline = time.monotonic() + PROBE_TIMEOUT
    last_data = time.monotonic()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=1.0)
            except asyncio.TimeoutError:
                chunk = b""
            if chunk:
                buf.extend(chunk)
                last_data = time.monotonic()
            else:
                if proc.returncode is not None:
                    break
                if buf and (time.monotonic() - last_data) > PROBE_IDLE:
                    break
            if time.monotonic() > deadline:
                break
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

    raw = strip_ansi(bytes(buf).decode("utf-8", errors="ignore"))
    log.debug("probe output:\n%s", raw)
    return _parse_tracks_table(raw)


def _parse_tracks_table(text: str) -> tuple[list[Track], list[Track], list[Track]]:
    """Parse the Spectre.Console table that N_m3u8DL-RE prints.

    Each data row is delimited by U+2502 (│). The first cell is the track type
    ("Vid"/"Aud"/"Sub"), the second cell is the track ID. We use the rest of
    the cells as a human-readable label for the inline button.
    """
    videos: list[Track] = []
    audios: list[Track] = []
    subs: list[Track] = []

    for line in text.splitlines():
        if "│" not in line:
            continue
        cells = [c.strip() for c in line.split("│")]
        # Drop empty edge cells produced by the leading/trailing │
        cells = [c for c in cells if c != ""]
        if len(cells) < 2:
            continue

        kind_raw = cells[0].lower()
        if kind_raw not in ("vid", "aud", "sub"):
            continue

        track_id = cells[1]
        # Skip header rows where ID column literally reads "ID"/"Type"/etc.
        if not track_id or track_id.lower() in ("id", "type", "codecs"):
            continue

        rest = [c for c in cells[2:] if c]
        label = " | ".join(rest)
        # Telegram button text limit ~ 64 chars; trim conservatively.
        if len(label) > 55:
            label = label[:52] + "..."
        if not label:
            label = track_id

        if kind_raw == "vid":
            videos.append(Track(len(videos), track_id, label, "vid"))
        elif kind_raw == "aud":
            audios.append(Track(len(audios), track_id, label, "aud"))
        else:
            subs.append(Track(len(subs), track_id, label, "sub"))

    return videos, audios, subs


# ---------------------------------------------------------------------------
# Inline keyboard builders
# ---------------------------------------------------------------------------

def _cb(short_id: str, step: str, value: str) -> str:
    # callback_data must be ≤ 64 bytes — short_id is 8, step 1, value is small.
    return f"j:{short_id}:{step}:{value}"


def kb_videos(job: Job) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🎬 {t.label}", callback_data=_cb(job.short_id, "v", str(t.idx)))]
        for t in job.videos
    ]
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data=_cb(job.short_id, "x", "0"))])
    return InlineKeyboardMarkup(rows)


def kb_audios(job: Job) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🎧 {t.label}", callback_data=_cb(job.short_id, "a", str(t.idx)))]
        for t in job.audios
    ]
    if job.audios:
        rows.append([InlineKeyboardButton("⭐ All audio tracks", callback_data=_cb(job.short_id, "a", "all"))])
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data=_cb(job.short_id, "x", "0"))])
    return InlineKeyboardMarkup(rows)


def kb_subs(job: Job) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"💬 {t.label}", callback_data=_cb(job.short_id, "s", str(t.idx)))]
        for t in job.subs
    ]
    if job.subs:
        rows.append([InlineKeyboardButton("⭐ All subtitles", callback_data=_cb(job.short_id, "s", "all"))])
    rows.append([
        InlineKeyboardButton("⏭ No subs", callback_data=_cb(job.short_id, "s", "none")),
        InlineKeyboardButton("✖ Cancel", callback_data=_cb(job.short_id, "x", "0")),
    ])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Status message helper (with FloodWait + MessageNotModified handling)
# ---------------------------------------------------------------------------

class StatusUpdater:
    """Edits the status message at most once per `interval` seconds."""

    def __init__(self, app: Client, chat_id: int, msg_id: int, interval: float):
        self.app = app
        self.chat_id = chat_id
        self.msg_id = msg_id
        self.interval = interval
        self._last_text = ""
        self._last_edit = 0.0

    async def update(self, text: str, force: bool = False, **kwargs) -> None:
        now = time.monotonic()
        if not force and (now - self._last_edit) < self.interval:
            return
        if text == self._last_text and not kwargs.get("reply_markup"):
            return
        try:
            await self.app.edit_message_text(self.chat_id, self.msg_id, text, **kwargs)
            self._last_text = text
            self._last_edit = now
        except MessageNotModified:
            self._last_edit = now
        except FloodWait as e:
            log.warning("FloodWait %ss while editing status", e.value)
            await asyncio.sleep(e.value + 1)
        except Exception as exc:
            log.warning("edit_message_text failed: %s", exc)


# ---------------------------------------------------------------------------
# Download phase
# ---------------------------------------------------------------------------

def _build_download_argv(job: Job) -> list[str]:
    """Compose the full N_m3u8DL-RE argv from the job's selections."""
    argv: list[str] = [NMDL_BIN, job.url]

    # Video: always exactly one track.
    argv += ["-sv", f"id={job.chosen_video}"]

    # Audio: "all" or a specific track. (We never offer "none" — N_m3u8DL-RE
    # would happily mux silence anyway and most users expect audio.)
    if job.chosen_audio == "all":
        argv += ["-sa", "all"]
    elif job.chosen_audio:
        argv += ["-sa", f"id={job.chosen_audio}"]

    # Subs: optional.
    if job.chosen_sub == "all":
        argv += ["-ss", "all"]
    elif job.chosen_sub and job.chosen_sub != "none":
        argv += ["-ss", f"id={job.chosen_sub}"]

    argv += job.headers
    argv += job.keys

    save_name = f"video_{job.short_id}"
    argv += [
        "--save-dir", str(job.work_dir),
        "--save-name", save_name,
        "--tmp-dir", str(job.work_dir / "tmp"),
        "--mux-after-done", "format=mkv",
        "--no-date-info",
        "--auto-select",
    ]
    return argv


async def _stream_lines(stream: asyncio.StreamReader):
    """Yield logical 'lines' from a stream that uses both '\\r' and '\\n'.

    N_m3u8DL-RE redraws progress with carriage returns, so readline() won't
    return until a real newline. We split on either separator.
    """
    buf = bytearray()
    while True:
        chunk = await stream.read(1024)
        if not chunk:
            if buf:
                yield bytes(buf).decode("utf-8", errors="ignore")
            return
        buf.extend(chunk)
        while True:
            idxs = [i for i in (buf.find(b"\r"), buf.find(b"\n")) if i != -1]
            if not idxs:
                break
            cut = min(idxs)
            line = bytes(buf[:cut]).decode("utf-8", errors="ignore")
            del buf[: cut + 1]
            if line.strip():
                yield line


async def _run_nmdl_with_progress(job: Job, status: StatusUpdater) -> None:
    argv = _build_download_argv(job)
    log.info("download argv: %s", " ".join(shlex.quote(a) for a in argv))

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(job.work_dir),
    )
    job.proc = proc

    last_pct = 0.0
    last_speed = ""
    last_size = ""
    last_phase = "Downloading"

    async def reader():
        nonlocal last_pct, last_speed, last_size, last_phase
        async for raw_line in _stream_lines(proc.stdout):
            line = strip_ansi(raw_line).strip()
            if not line:
                continue

            # Coarse phase detection from log keywords.
            low = line.lower()
            if "muxing" in low or "binary merge" in low or "ffmpeg" in low:
                last_phase = "Muxing"
            elif "downloading" in low or "fetching" in low:
                last_phase = "Downloading"
            elif "decrypt" in low:
                last_phase = "Decrypting"

            m_pct = PCT_RE.findall(line)
            if m_pct:
                # Take the largest percentage on the line (covers multi-bar rows).
                try:
                    last_pct = max(float(p) for p in m_pct)
                except ValueError:
                    pass

            m_speed = SPEED_RE.search(line)
            if m_speed:
                last_speed = f"{m_speed.group(1)} {m_speed.group(2)}"

            m_size = SIZE_RE.search(line)
            if m_size:
                last_size = f"{m_size.group(1)} {m_size.group(2)} / {m_size.group(3)} {m_size.group(4)}"

            if log.isEnabledFor(logging.DEBUG):
                log.debug("nmdl> %s", line)

    async def ticker():
        while proc.returncode is None:
            text = (
                f"⬇️ <b>{last_phase}</b>\n"
                f"<code>{progress_bar(last_pct)}</code>\n"
            )
            if last_size:
                text += f"📦 {last_size}\n"
            if last_speed:
                text += f"🚀 {last_speed}\n"
            text += f"\n<code>/cancel</code> to abort"
            await status.update(text)
            await asyncio.sleep(PROGRESS_INTERVAL)

    reader_task = asyncio.create_task(reader())
    ticker_task = asyncio.create_task(ticker())

    try:
        await asyncio.wait_for(proc.wait(), timeout=DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        log.error("download timed out after %ss", DOWNLOAD_TIMEOUT)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise RuntimeError(f"Download exceeded {DOWNLOAD_TIMEOUT:.0f}s timeout")
    finally:
        ticker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker_task
        with contextlib.suppress(Exception):
            await reader_task

    if job.cancelled:
        raise RuntimeError("Cancelled by user")
    if proc.returncode != 0:
        raise RuntimeError(f"N_m3u8DL-RE exited with code {proc.returncode}")


# ---------------------------------------------------------------------------
# Output discovery + thumbnail + upload
# ---------------------------------------------------------------------------

def _find_output_file(job: Job) -> Path:
    """Pick the most plausible final media file inside job.work_dir."""
    preferred_suffixes = (".mkv", ".mp4", ".m4a", ".webm", ".ts")
    candidates: list[Path] = []
    for ext in preferred_suffixes:
        candidates.extend(p for p in job.work_dir.glob(f"*{ext}") if p.is_file())
        if candidates:
            break
    if not candidates:
        # Fall back to the largest file overall, ignoring tmp dir.
        for p in job.work_dir.iterdir():
            if p.is_file():
                candidates.append(p)
    if not candidates:
        raise FileNotFoundError("N_m3u8DL-RE produced no output file")
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]


async def _generate_thumbnail(media: Path, out: Path) -> Optional[Path]:
    """Grab a 320-px-wide JPEG from the 10-second mark with ffmpeg."""
    argv = [
        FFMPEG_BIN, "-y",
        "-ss", "00:00:10",
        "-i", str(media),
        "-frames:v", "1",
        "-vf", "scale=320:-2",
        "-q:v", "5",
        str(out),
    ]
    log.info("thumbnail argv: %s", " ".join(shlex.quote(a) for a in argv))
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            log.warning("thumbnail generation timed out")
            return None
        if proc.returncode != 0:
            log.warning("ffmpeg failed (rc=%s): %s",
                        proc.returncode, (err or b"").decode(errors="ignore"))
            return None
        if not out.exists() or out.stat().st_size == 0:
            return None
        return out
    except FileNotFoundError:
        log.warning("ffmpeg binary not found at %s — skipping thumbnail", FFMPEG_BIN)
        return None


async def _upload(app: Client, job: Job, media: Path,
                  thumb: Optional[Path], status: StatusUpdater) -> None:
    size = media.stat().st_size
    last_edit = 0.0

    async def progress(current: int, total: int):
        nonlocal last_edit
        now = time.monotonic()
        if now - last_edit < PROGRESS_INTERVAL:
            return
        last_edit = now
        pct = (current / total * 100.0) if total else 0.0
        text = (
            f"⬆️ <b>Uploading</b>\n"
            f"<code>{progress_bar(pct)}</code>\n"
            f"📦 {human_bytes(current)} / {human_bytes(total)}"
        )
        await status.update(text, force=True)

    caption = (
        f"<b>{media.name}</b>\n"
        f"📦 {human_bytes(size)}\n"
        f"🎬 <code>id={job.chosen_video}</code>"
    )

    await app.send_document(
        chat_id=job.chat_id,
        document=str(media),
        thumb=str(thumb) if thumb else None,
        file_name=media.name,
        caption=caption,
        progress=progress,
    )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup(job: Job) -> None:
    """Wipe the per-job working directory. Always safe to call."""
    JOBS.pop(job.short_id, None)
    if job.work_dir and job.work_dir.exists():
        try:
            shutil.rmtree(job.work_dir, ignore_errors=True)
            log.info("cleaned up %s", job.work_dir)
        except Exception as e:
            log.warning("cleanup failed for %s: %s", job.work_dir, e)


# ---------------------------------------------------------------------------
# Job orchestrator (semaphore + queueing)
# ---------------------------------------------------------------------------

async def run_job(app: Client, job: Job) -> None:
    """Acquire the global slot, run the download, upload, and clean up."""
    global ACTIVE, WAITING

    status = StatusUpdater(app, job.chat_id, job.status_msg_id, PROGRESS_INTERVAL)

    if ACTIVE >= MAX_CONCURRENT:
        WAITING += 1
        await status.update(
            f"⏳ <b>Queued…</b>\nPosition: {WAITING}\nActive workers: {ACTIVE}/{MAX_CONCURRENT}",
            force=True,
        )

    await SEMAPHORE.acquire()
    if WAITING > 0:
        WAITING -= 1
    ACTIVE += 1

    try:
        await status.update("🚀 <b>Starting download…</b>", force=True)
        await _run_nmdl_with_progress(job, status)

        media = _find_output_file(job)
        await status.update(
            f"🖼️ <b>Generating thumbnail…</b>\nFile: <code>{media.name}</code>",
            force=True,
        )
        thumb_path = job.work_dir / "thumb.jpg"
        thumb = await _generate_thumbnail(media, thumb_path)

        await status.update(
            f"⬆️ <b>Uploading…</b>\nFile: <code>{media.name}</code>\n📦 {human_bytes(media.stat().st_size)}",
            force=True,
        )
        await _upload(app, job, media, thumb, status)

        await status.update(
            f"✅ <b>Done</b>\n<code>{media.name}</code>",
            force=True,
        )
    except Exception as exc:
        log.exception("job %s failed", job.short_id)
        await status.update(
            f"❌ <b>Failed</b>\n<code>{type(exc).__name__}: {exc}</code>",
            force=True,
        )
    finally:
        ACTIVE = max(0, ACTIVE - 1)
        SEMAPHORE.release()
        cleanup(job)


# ---------------------------------------------------------------------------
# Pyrogram client + handlers
# ---------------------------------------------------------------------------

app = Client(
    SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=ParseMode.HTML,
    workdir=str(DOWNLOAD_ROOT.parent),
)


HELP_TEXT = (
    "<b>nmdl-bot</b>\n\n"
    "Send a Widevine/HLS/DASH manifest with N_m3u8DL-RE-style flags:\n\n"
    "<code>/dl N_m3u8DL-RE \"https://host/manifest.mpd\" "
    "-H \"User-Agent: Mozilla/5.0\" --key KID:KEY</code>\n\n"
    "Multiple <code>-H</code> and <code>--key</code> arguments are supported.\n"
    "After parsing the manifest you'll pick Video → Audio → Subtitles, "
    "and the bot will download, decrypt, mux to MKV, and upload back.\n\n"
    f"Concurrency limit: <b>{MAX_CONCURRENT}</b> simultaneous downloads."
)


@app.on_message(filters.command(["start", "help"]) & filters.private)
async def cmd_help(client: Client, message: Message):
    await message.reply_text(HELP_TEXT, disable_web_page_preview=True)


@app.on_message(filters.command("cancel") & filters.private)
async def cmd_cancel(client: Client, message: Message):
    """Cancel any active job owned by this user."""
    cancelled = 0
    for job in list(JOBS.values()):
        if job.user_id != message.from_user.id:
            continue
        job.cancelled = True
        if job.proc and job.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                job.proc.terminate()
        cancelled += 1
    await message.reply_text(
        f"🛑 Cancelled {cancelled} job(s)." if cancelled else "Nothing to cancel."
    )


@app.on_message(filters.command("dl") & filters.private)
async def cmd_dl(client: Client, message: Message):
    raw = (message.text or "").split(None, 1)
    if len(raw) < 2:
        await message.reply_text(
            "Usage:\n<code>/dl N_m3u8DL-RE \"URL\" -H \"...\" --key KID:KEY</code>",
        )
        return

    try:
        url, headers, keys = parse_command(raw[1])
    except ValueError as e:
        await message.reply_text(f"❌ Parse error: <code>{e}</code>")
        return

    job_id = uuid.uuid4().hex
    short_id = job_id[:8]
    work_dir = DOWNLOAD_ROOT / short_id
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "tmp").mkdir(exist_ok=True)

    status_msg = await message.reply_text(
        f"🔍 <b>Probing manifest…</b>\n<code>{url[:200]}</code>",
        disable_web_page_preview=True,
    )

    job = Job(
        job_id=job_id,
        short_id=short_id,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        status_msg_id=status_msg.id,
        url=url,
        headers=headers,
        keys=keys,
        work_dir=work_dir,
    )
    JOBS[short_id] = job

    try:
        videos, audios, subs = await probe_tracks(url, headers, keys, work_dir)
    except Exception as e:
        log.exception("probe failed")
        await status_msg.edit_text(
            f"❌ <b>Probe failed</b>\n<code>{type(e).__name__}: {e}</code>",
        )
        cleanup(job)
        return

    job.videos, job.audios, job.subs = videos, audios, subs

    if not videos:
        await status_msg.edit_text(
            "❌ <b>No video tracks found.</b>\n"
            "Either the manifest is unreachable or the bot couldn't parse "
            "N_m3u8DL-RE's table output. Check headers/keys and try again.",
        )
        cleanup(job)
        return

    await status_msg.edit_text(
        f"🎬 <b>Pick a video quality</b>\n"
        f"<i>{len(videos)} video / {len(audios)} audio / {len(subs)} sub track(s)</i>",
        reply_markup=kb_videos(job),
    )


@app.on_callback_query(filters.regex(r"^j:[0-9a-f]{8}:[vasx]:"))
async def on_choice(client: Client, cq: CallbackQuery):
    try:
        _, short_id, step, value = cq.data.split(":", 3)
    except ValueError:
        await cq.answer("Bad callback", show_alert=True)
        return

    job = JOBS.get(short_id)
    if not job:
        await cq.answer("Job expired.", show_alert=True)
        with contextlib.suppress(Exception):
            await cq.message.edit_text("⌛ This job expired. Send /dl again.")
        return
    if cq.from_user.id != job.user_id:
        await cq.answer("Not your job.", show_alert=True)
        return

    # Cancel
    if step == "x":
        job.cancelled = True
        if job.proc and job.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                job.proc.terminate()
        cleanup(job)
        await cq.answer("Cancelled.")
        with contextlib.suppress(Exception):
            await cq.message.edit_text("🛑 Cancelled.")
        return

    # Resolve track choice
    def resolve(value: str, tracks: list[Track]) -> Optional[str]:
        if value in ("all", "none"):
            return value
        try:
            idx = int(value)
        except ValueError:
            return None
        if 0 <= idx < len(tracks):
            return tracks[idx].track_id
        return None

    if step == "v":
        tid = resolve(value, job.videos)
        if not tid or tid in ("all", "none"):
            await cq.answer("Bad selection", show_alert=True)
            return
        job.chosen_video = tid
        await cq.answer("Video set")

        if job.audios:
            await cq.message.edit_text(
                f"🎬 Video: <code>{tid}</code>\n\n🎧 <b>Pick an audio track</b>",
                reply_markup=kb_audios(job),
            )
        else:
            # No audio tracks listed → skip directly to subs (or download).
            if job.subs:
                await cq.message.edit_text(
                    f"🎬 Video: <code>{tid}</code>\n\n💬 <b>Pick subtitles</b>",
                    reply_markup=kb_subs(job),
                )
            else:
                asyncio.create_task(run_job(client, job))
        return

    if step == "a":
        tid = resolve(value, job.audios) if value not in ("all",) else "all"
        if tid is None:
            await cq.answer("Bad selection", show_alert=True)
            return
        job.chosen_audio = tid
        await cq.answer("Audio set")

        if job.subs:
            await cq.message.edit_text(
                f"🎬 Video: <code>{job.chosen_video}</code>\n"
                f"🎧 Audio: <code>{tid}</code>\n\n"
                f"💬 <b>Pick subtitles</b>",
                reply_markup=kb_subs(job),
            )
        else:
            asyncio.create_task(run_job(client, job))
        return

    if step == "s":
        if value in ("all", "none"):
            tid = value
        else:
            tid = resolve(value, job.subs)
            if tid is None:
                await cq.answer("Bad selection", show_alert=True)
                return
        job.chosen_sub = tid
        await cq.answer("Subtitles set")

        await cq.message.edit_text(
            f"🎬 Video: <code>{job.chosen_video}</code>\n"
            f"🎧 Audio: <code>{job.chosen_audio}</code>\n"
            f"💬 Subs:  <code>{job.chosen_sub}</code>\n\n"
            f"⏳ <b>Adding to queue…</b>",
        )
        asyncio.create_task(run_job(client, job))
        return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("nmdl-bot starting (concurrency=%d, root=%s)", MAX_CONCURRENT, DOWNLOAD_ROOT)
    app.run()
