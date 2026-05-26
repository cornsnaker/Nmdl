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
import collections
import contextlib
import html
import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import sys
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
PAGE_SIZE = max(1, int(os.getenv("PAGE_SIZE", "6")))              # buttons / page

OWNER_ID = int(os.getenv("OWNER_ID", "0"))                        # required for owner cmds
AUTH_FILE = Path(os.getenv("AUTH_FILE", "./auth_users.json")).resolve()
LOG_FILE = Path(os.getenv("LOG_FILE", "./bot.log")).resolve()
LOG_BUFFER = max(50, int(os.getenv("LOG_BUFFER", "1000")))        # ring buffer size
SHELL_TIMEOUT = float(os.getenv("SHELL_TIMEOUT", "60"))           # /shell hard cap

if not (API_ID and API_HASH and BOT_TOKEN):
    raise SystemExit("API_ID, API_HASH and BOT_TOKEN env vars must be set")
if OWNER_ID == 0:
    # Not fatal — bot still works for the public /dl flow, but owner cmds are disabled.
    logging.getLogger("nmdl-bot").warning(
        "OWNER_ID not set — /auth, /unauth, /restart, /logs, /shell will be disabled"
    )

DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format=_LOG_FMT)
log = logging.getLogger("nmdl-bot")


class _RingHandler(logging.Handler):
    """Keeps the last N formatted log lines in memory for /logs."""

    def __init__(self, capacity: int):
        super().__init__()
        self.buf: collections.deque[str] = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buf.append(self.format(record))
        except Exception:  # never let logging crash the bot
            pass


_log_formatter = logging.Formatter(_LOG_FMT)

LOG_RING = _RingHandler(LOG_BUFFER)
LOG_RING.setFormatter(_log_formatter)
logging.getLogger().addHandler(LOG_RING)

try:
    _file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_handler.setFormatter(_log_formatter)
    logging.getLogger().addHandler(_file_handler)
except Exception as _e:  # e.g. read-only fs
    log.warning("Could not attach file log handler at %s: %s", LOG_FILE, _e)

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


# --- filename sanitisation ---------------------------------------------------

_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

def sanitize_filename(raw: str, fallback: str = "video") -> str:
    """Clean a user-provided filename: strip path separators, control chars,
    leading/trailing whitespace and dots, cap length to 200 chars."""
    if not raw:
        return fallback
    cleaned = _BAD_NAME_CHARS.sub("_", raw).strip().strip(".")
    cleaned = cleaned[:200].strip()
    return cleaned or fallback


# --- auth (owner + authorized user list) ------------------------------------

AUTH_USERS: set[int] = set()


def _load_auth() -> None:
    global AUTH_USERS
    try:
        if AUTH_FILE.exists():
            data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            AUTH_USERS = {int(x) for x in data}
            log.info("loaded %d authorized user(s) from %s", len(AUTH_USERS), AUTH_FILE)
    except Exception as e:
        log.warning("failed to load auth file %s: %s", AUTH_FILE, e)
        AUTH_USERS = set()


def _save_auth() -> None:
    try:
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTH_FILE.write_text(json.dumps(sorted(AUTH_USERS)), encoding="utf-8")
    except Exception as e:
        log.warning("failed to save auth file %s: %s", AUTH_FILE, e)


def _is_owner(uid: Optional[int]) -> bool:
    return bool(OWNER_ID) and uid == OWNER_ID


def _is_authorized(uid: Optional[int]) -> bool:
    if not uid:
        return False
    return _is_owner(uid) or uid in AUTH_USERS


_load_auth()


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

    # FSM bit flipped between subs choice and the user's filename text reply.
    awaiting_name: bool = False
    custom_name: Optional[str] = None     # sanitized base name (no extension)

    # Current page index for each step's keyboard (0-based).
    page_v: int = 0
    page_a: int = 0
    page_s: int = 0

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


def _paginate(items: list, page: int) -> tuple[list, int, int]:
    """Slice `items` for `page`. Returns (window, clamped_page, total_pages)."""
    total = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total - 1))
    start = page * PAGE_SIZE
    return items[start : start + PAGE_SIZE], page, total


def _nav_row(short_id: str, nav_step: str, page: int, total: int) -> Optional[list]:
    """Prev / page-indicator / Next row. `nav_step` is uppercase (V/A/S)."""
    if total <= 1:
        return None
    prev_p = (page - 1) % total
    next_p = (page + 1) % total
    return [
        InlineKeyboardButton("« Prev", callback_data=_cb(short_id, nav_step, str(prev_p))),
        InlineKeyboardButton(f"{page + 1}/{total}", callback_data=_cb(short_id, "n", "0")),
        InlineKeyboardButton("Next »", callback_data=_cb(short_id, nav_step, str(next_p))),
    ]


def kb_videos(job: Job) -> InlineKeyboardMarkup:
    window, job.page_v, total = _paginate(job.videos, job.page_v)
    rows = [
        [InlineKeyboardButton(f"🎬 {t.label}", callback_data=_cb(job.short_id, "v", str(t.idx)))]
        for t in window
    ]
    nav = _nav_row(job.short_id, "V", job.page_v, total)
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data=_cb(job.short_id, "x", "0"))])
    return InlineKeyboardMarkup(rows)


def kb_audios(job: Job) -> InlineKeyboardMarkup:
    window, job.page_a, total = _paginate(job.audios, job.page_a)
    rows = [
        [InlineKeyboardButton(f"🎧 {t.label}", callback_data=_cb(job.short_id, "a", str(t.idx)))]
        for t in window
    ]
    nav = _nav_row(job.short_id, "A", job.page_a, total)
    if nav:
        rows.append(nav)
    if job.audios:
        rows.append([InlineKeyboardButton("⭐ All audio tracks", callback_data=_cb(job.short_id, "a", "all"))])
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data=_cb(job.short_id, "x", "0"))])
    return InlineKeyboardMarkup(rows)


def kb_subs(job: Job) -> InlineKeyboardMarkup:
    window, job.page_s, total = _paginate(job.subs, job.page_s)
    rows = [
        [InlineKeyboardButton(f"💬 {t.label}", callback_data=_cb(job.short_id, "s", str(t.idx)))]
        for t in window
    ]
    nav = _nav_row(job.short_id, "S", job.page_s, total)
    if nav:
        rows.append(nav)
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
        # Apply user-chosen name if any.
        if job.custom_name:
            target = media.with_name(f"{job.custom_name}{media.suffix}")
            try:
                if target.exists():
                    target.unlink()
                media.rename(target)
                media = target
                log.info("renamed output to %s", media.name)
            except OSError as e:
                log.warning("rename to %s failed: %s — keeping original", target.name, e)
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
    "After the manifest is parsed you'll pick Video → Audio → Subtitles "
    "(paginated when there are many tracks), then enter a custom file name "
    "(or <code>/skip</code> for the default). The bot downloads, decrypts, "
    "muxes to MKV, and uploads back.\n\n"
    "<b>Commands</b>\n"
    "  /dl       — start a download\n"
    "  /skip     — keep the auto-generated file name\n"
    "  /cancel   — abort your active job\n"
    "  /help     — show this help\n\n"
    f"Concurrency limit: <b>{MAX_CONCURRENT}</b> simultaneous downloads."
)

OWNER_HELP_TEXT = (
    "<b>Owner commands</b>\n"
    "  /auth &lt;user_id&gt;   — grant access\n"
    "  /unauth &lt;user_id&gt; — revoke access\n"
    "  /authlist           — list authorized users\n"
    "  /logs [N]           — last N log lines (default 50)\n"
    "  /shell &lt;cmd&gt;       — run a shell command on the host\n"
    "  /restart            — restart the bot process"
)


@app.on_message(filters.command(["start", "help"]) & filters.private)
async def cmd_help(client: Client, message: Message):
    body = HELP_TEXT
    if not _is_authorized(message.from_user.id):
        body += (
            "\n\n⛔ <b>You are not authorized.</b> "
            f"Send your user id <code>{message.from_user.id}</code> to the owner "
            "and ask them to <code>/auth</code> you."
        )
    if _is_owner(message.from_user.id):
        body += "\n\n" + OWNER_HELP_TEXT
    await message.reply_text(body, disable_web_page_preview=True)


@app.on_message(filters.command("cancel") & filters.private)
async def cmd_cancel(client: Client, message: Message):
    """Cancel any active job owned by this user."""
    if not _is_authorized(message.from_user.id):
        await message.reply_text("⛔ Not authorized.")
        return
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
    if not _is_authorized(message.from_user.id):
        await message.reply_text(
            f"⛔ Not authorized. Your id: <code>{message.from_user.id}</code>"
        )
        return
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


@app.on_callback_query(filters.regex(r"^j:[0-9a-f]{8}:[vasxVASn]:"))
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

    # Inert page-indicator button.
    if step == "n":
        await cq.answer()
        return

    # Page navigation: V / A / S → re-render the same step's keyboard.
    if step in ("V", "A", "S"):
        try:
            new_page = int(value)
        except ValueError:
            await cq.answer("Bad page", show_alert=True)
            return
        if step == "V":
            job.page_v = new_page
            kb = kb_videos(job)
        elif step == "A":
            job.page_a = new_page
            kb = kb_audios(job)
        else:
            job.page_s = new_page
            kb = kb_subs(job)
        with contextlib.suppress(MessageNotModified):
            await cq.message.edit_reply_markup(reply_markup=kb)
        await cq.answer()
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
            # No audio tracks listed → skip directly to subs / filename prompt.
            if job.subs:
                await cq.message.edit_text(
                    f"🎬 Video: <code>{tid}</code>\n\n💬 <b>Pick subtitles</b>",
                    reply_markup=kb_subs(job),
                )
            else:
                await _enter_name_step(cq.message, job)
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
            await _enter_name_step(cq.message, job)
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
        await _enter_name_step(cq.message, job)
        return


async def _enter_name_step(message: Message, job: Job) -> None:
    """Prompt the user for a custom filename. Their next text message — or
    /skip — will resume the job."""
    # Only one job per user can be awaiting a name at a time. Drop the flag
    # from any previous awaiting jobs by the same user.
    for j in JOBS.values():
        if j.user_id == job.user_id and j is not job:
            j.awaiting_name = False
    job.awaiting_name = True
    summary = (
        f"🎬 Video: <code>{job.chosen_video}</code>\n"
        f"🎧 Audio: <code>{job.chosen_audio or 'auto'}</code>\n"
        f"💬 Subs:  <code>{job.chosen_sub or 'none'}</code>\n\n"
        f"📝 <b>Send a file name</b> (no extension), "
        f"or /skip to keep the default."
    )
    with contextlib.suppress(Exception):
        await message.edit_text(summary)


def _find_awaiting_job(user_id: int) -> Optional[Job]:
    return next(
        (j for j in JOBS.values() if j.user_id == user_id and j.awaiting_name),
        None,
    )


@app.on_message(filters.command("skip") & filters.private)
async def cmd_skip(client: Client, message: Message):
    """Resume an awaiting-name job using the auto-generated filename."""
    job = _find_awaiting_job(message.from_user.id)
    if not job:
        await message.reply_text("Nothing waiting for a name.")
        return
    job.awaiting_name = False
    job.custom_name = None
    await message.reply_text("⏭ Using default name. ⏳ Queueing…")
    asyncio.create_task(run_job(client, job))


@app.on_message(filters.private & filters.text & ~filters.regex(r"^/"))
async def on_text(client: Client, message: Message):
    """Capture a filename if the user has a job waiting on it."""
    job = _find_awaiting_job(message.from_user.id)
    if not job:
        return
    job.custom_name = sanitize_filename(message.text or "")
    job.awaiting_name = False
    await message.reply_text(
        f"📝 Filename: <code>{html.escape(job.custom_name)}</code>\n⏳ Queueing…"
    )
    asyncio.create_task(run_job(client, job))


# ---------------------------------------------------------------------------
# Owner-only commands
# ---------------------------------------------------------------------------

def _parse_uid_arg(message: Message) -> Optional[int]:
    parts = (message.text or "").split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


@app.on_message(filters.command("auth") & filters.private)
async def cmd_auth(client: Client, message: Message):
    if not _is_owner(message.from_user.id):
        await message.reply_text("⛔ Owner only.")
        return
    uid = _parse_uid_arg(message)
    if uid is None:
        await message.reply_text("Usage: <code>/auth &lt;user_id&gt;</code>")
        return
    AUTH_USERS.add(uid)
    _save_auth()
    await message.reply_text(f"✅ Authorized <code>{uid}</code>.")


@app.on_message(filters.command("unauth") & filters.private)
async def cmd_unauth(client: Client, message: Message):
    if not _is_owner(message.from_user.id):
        await message.reply_text("⛔ Owner only.")
        return
    uid = _parse_uid_arg(message)
    if uid is None:
        await message.reply_text("Usage: <code>/unauth &lt;user_id&gt;</code>")
        return
    if uid in AUTH_USERS:
        AUTH_USERS.discard(uid)
        _save_auth()
        await message.reply_text(f"🚫 Revoked <code>{uid}</code>.")
    else:
        await message.reply_text(f"<code>{uid}</code> wasn't authorized.")


@app.on_message(filters.command("authlist") & filters.private)
async def cmd_authlist(client: Client, message: Message):
    if not _is_owner(message.from_user.id):
        await message.reply_text("⛔ Owner only.")
        return
    if not AUTH_USERS:
        await message.reply_text("(no authorized users)")
        return
    body = "\n".join(f"• <code>{u}</code>" for u in sorted(AUTH_USERS))
    await message.reply_text(f"<b>Authorized users</b>\n{body}")


@app.on_message(filters.command("logs") & filters.private)
async def cmd_logs(client: Client, message: Message):
    if not _is_owner(message.from_user.id):
        await message.reply_text("⛔ Owner only.")
        return
    parts = (message.text or "").split()
    n = 50
    if len(parts) > 1 and parts[1].isdigit():
        n = max(1, min(int(parts[1]), LOG_BUFFER))
    lines = list(LOG_RING.buf)[-n:]
    text = "\n".join(lines) if lines else "(no logs)"
    if len(text) <= 3500:
        await message.reply_text(f"<pre>{html.escape(text)}</pre>")
        return
    # Too long — send as a file.
    tmp = DOWNLOAD_ROOT / f"logs_{int(time.time())}.txt"
    try:
        tmp.write_text(text, encoding="utf-8")
        await message.reply_document(str(tmp), caption=f"Last {n} log lines")
    finally:
        with contextlib.suppress(Exception):
            tmp.unlink()


@app.on_message(filters.command("shell") & filters.private)
async def cmd_shell(client: Client, message: Message):
    if not _is_owner(message.from_user.id):
        await message.reply_text("⛔ Owner only.")
        return
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("Usage: <code>/shell &lt;command&gt;</code>")
        return
    cmd = parts[1]
    log.warning("owner shell: %s", cmd)
    note = await message.reply_text(f"⚙️ Running…\n<code>{html.escape(cmd)}</code>")

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        await note.edit_text(
            f"⏱ Timed out after {SHELL_TIMEOUT:.0f}s\n<code>{html.escape(cmd)}</code>"
        )
        return

    output = stdout.decode("utf-8", errors="ignore") if stdout else ""
    rc = proc.returncode
    summary_head = f"⚙️ <code>{html.escape(cmd)}</code>\nrc=<b>{rc}</b>\n"

    full = f"{summary_head}\n<pre>{html.escape(output) or '(no output)'}</pre>"
    if len(full) <= 3500:
        await note.edit_text(full)
        return

    tmp = DOWNLOAD_ROOT / f"shell_{int(time.time())}.txt"
    try:
        tmp.write_text(output, encoding="utf-8")
        await note.edit_text(summary_head + "(output attached)")
        await message.reply_document(str(tmp), caption=f"rc={rc}")
    finally:
        with contextlib.suppress(Exception):
            tmp.unlink()


@app.on_message(filters.command("restart") & filters.private)
async def cmd_restart(client: Client, message: Message):
    if not _is_owner(message.from_user.id):
        await message.reply_text("⛔ Owner only.")
        return
    await message.reply_text("♻️ Restarting…")
    log.warning("owner-triggered restart")
    # Best-effort: terminate any in-flight subprocesses so they don't leak.
    for j in list(JOBS.values()):
        j.cancelled = True
        if j.proc and j.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                j.proc.terminate()
    # Give Telegram a moment to flush the outgoing reply, then exec ourselves.
    await asyncio.sleep(1.0)
    os.execv(sys.executable, [sys.executable, *sys.argv])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("nmdl-bot starting (concurrency=%d, root=%s)", MAX_CONCURRENT, DOWNLOAD_ROOT)
    app.run()
