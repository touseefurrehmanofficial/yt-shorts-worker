"""Orchestrates one batch: picks the next N shuffled 'remaining' reels,
downloads each from Facebook, edits it (title bar + moving watermark +
original-music-free synthesized background audio), uploads it to YouTube
scheduled SCHEDULE_INTERVAL apart starting at
--start-date/--upload-time, updates data/reels.csv, and deletes the local
video files to free disk space once a reel is successfully scheduled.

Usage:
    python pipeline.py --batch-size 5 --start-date 2026-08-01
"""
import argparse
import csv
import datetime as dt
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import singleton_lock
from video_editor import edit_video
from yt_uploader import upload_and_schedule

CSV_PATH = ROOT.parent / "data" / "reels.csv"
DOWNLOADS = ROOT.parent / "downloads"
EDITED = ROOT.parent / "edited"
LOG_PATH = ROOT.parent / "logs" / "pipeline.log"
YT_DLP = ROOT.parent / "venv" / "Scripts" / "yt-dlp.exe"


def _ytdlp_cmd():
    """Local runs use the venv copy; cloud runners get yt-dlp on PATH from
    pip install (setup-python puts the Scripts dir on PATH)."""
    if YT_DLP.exists():
        return str(YT_DLP)
    return "yt-dlp"

DOWNLOAD_TIMEOUT_SECONDS = 900  # lenient: slow-but-progressing downloads are fine

FIELDNAMES = [
    "reel_id", "reel_url", "source_profile", "date_added", "status",
    "remaining_order", "video_name", "batch_number", "scheduled_date",
    "youtube_video_id",
]

NAME_RE = re.compile(r"Pure Talent (\d+)")
MAX_ATTEMPT_MULTIPLIER = 3
DEFAULT_UPLOAD_TIME = "10:00"
SCHEDULE_INTERVAL = dt.timedelta(hours=5)
MAX_SHORT_SECONDS = 178  # stay safely under YouTube's 180s Shorts cutoff
MIN_REEL_SECONDS = 30     # skip reels shorter than this (too brief to be useful)

# Errors matching these are permanent for a given reel (the source is gone,
# private, or otherwise unextractable) -- retrying it in every future batch
# would just fail identically forever, so mark it 'failed' instead of
# 'remaining' the first time one of these shows up.
PERMANENT_FAILURE_MARKERS = (
    "Cannot parse data",
)

# If this many candidates in a row fail, something structural broke (a
# YouTube upload limit/verification prompt, an expired login session, a
# Studio UI change, etc.) rather than a handful of individually-bad reels.
# Stop the batch instead of burning hours retrying candidates that will all
# fail the same way.
CONSECUTIVE_FAILURE_LIMIT = 3


def _is_permanent_failure(exc: Exception) -> bool:
    return any(marker in str(exc) for marker in PERMANENT_FAILURE_MARKERS)


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def log(msg: str):
    line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Diagnostic text scraped from Studio pages (e.g. a viewer comment
        # caught in a page-text dump) can contain characters the Windows
        # console's codepage can't print. That must never take down the
        # whole batch -- fall back to a lossy-but-safe re-encode.
        encoding = sys.stdout.encoding or "utf-8"
        safe = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, flush=True)
    _LAST_LOG["t"] = time.monotonic()


_LAST_LOG = {"t": 0.0}

WATCHDOG_SILENCE_SECONDS = 12 * 60  # no log line for 12 min => something is stuck


def _start_watchdog():
    """If the pipeline stops producing ANY log output for a long time (yt-dlp
    hangs, browser automation wedges, a subprocess pipe never reaches EOF),
    exit loudly instead of silently stalling the batch forever. Anything
    half-done is still 'remaining' in the CSV and will be retried on relaunch."""
    import threading

    def watch():
        while True:
            time.sleep(60)
            if time.monotonic() - _LAST_LOG["t"] > WATCHDOG_SILENCE_SECONDS:
                log(f"WATCHDOG: no log activity for {WATCHDOG_SILENCE_SECONDS // 60} minutes -- "
                    f"pipeline appears stuck. Exiting so it can be relaunched; unfinished "
                    f"reels stay 'remaining' and will be retried.")
                os._exit(1)

    threading.Thread(target=watch, daemon=True).start()


def _cleanup_orphaned_files():
    """Removes any leftover files in downloads/ or edited/ from a previous
    run that got killed mid-item (crash, force-quit, power loss, etc).
    Safe to always wipe: a reel only ever gets marked 'uploaded' in the CSV
    -- and thus considered done -- AFTER upload_and_schedule() returns
    successfully, so anything still sitting here was never counted as
    complete and the reel itself is still 'remaining' and will be
    re-downloaded fresh if picked again."""
    removed = 0
    for folder in (DOWNLOADS, EDITED):
        for f in folder.glob("*"):
            if f.is_file():
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    if removed:
        log(f"Cleaned up {removed} orphaned file(s) left over from an interrupted previous run.")


def load_rows():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_rows(rows):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in FIELDNAMES})


def next_video_number(rows):
    best = 0
    for r in rows:
        m = NAME_RE.search(r.get("video_name") or "")
        if m:
            best = max(best, int(m.group(1)))
    return best + 1


def next_batch_number(rows):
    nums = [int(r["batch_number"]) for r in rows if (r.get("batch_number") or "").isdigit()]
    return (max(nums) + 1) if nums else 1


def _json_to_netscape(json_path: Path, out_path: Path):
    """Convert Playwright-format cookie JSON to a Netscape cookies.txt file
    for yt-dlp."""
    import json as _json
    cookies = _json.loads(json_path.read_text())
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c.get("domain", "")
        if not domain.startswith(".") and not domain.startswith("http"):
            domain = "." + domain
        include = c.get("httpOnly", False)
        expiry = int(c.get("expires", 0) or 0)
        if expiry <= 0:
            expiry = 2147483647
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.get("path", "/"),
            "TRUE" if include else "FALSE",
            str(expiry),
            c.get("name", ""),
            c.get("value", ""),
        ]))
    out_path.write_text("\n".join(lines), encoding="utf-8")


def download_reel(reel_url: str, reel_id: str, fb_cookies_file: Path | None = None) -> Path:
    out_path = DOWNLOADS / f"{reel_id}.mp4"
    cookies_args = []
    netscape_file = None
    if fb_cookies_file is not None:
        # cloud mode: JSON (Playwright format) -> temp Netscape file for yt-dlp
        if fb_cookies_file.suffix.lower() == ".json":
            netscape_file = ROOT.parent / "data" / "_fb_cookies_netscape.txt"
            _json_to_netscape(fb_cookies_file, netscape_file)
            fb_cookies_file = netscape_file
        cookies_args = ["--cookies", str(fb_cookies_file)]
    else:
        cookies_args = [
            "--cookies-from-browser",
            "chrome:" + str(ROOT.parent / "automation_profiles" / "facebook_profile"),
        ]
    cmd = [
        _ytdlp_cmd(),
        *cookies_args,
        "-f", "bv*+ba/b",
        "-S", "res:720,vbr,tbr",  # prefer <=720p, fall back to best available
        "-S", "res,vbr,tbr",  # always prefer the highest real resolution/bitrate
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        reel_url,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    elapsed = 0
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=60)
                break
            except subprocess.TimeoutExpired:
                elapsed += 60
                size_mb = out_path.stat().st_size / 1e6 if out_path.exists() else 0.0
                log(f"  download in progress ({elapsed}s elapsed, {size_mb:.1f} MB) ...")
        result = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        # kill the whole tree: this machine duplicates venv processes into
        # system-python twins, so a plain .kill() would leave the twin running
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        raise RuntimeError(
            f"yt-dlp timed out after {DOWNLOAD_TIMEOUT_SECONDS}s for {reel_url}") from None
    if result.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"yt-dlp failed for {reel_url}:\n{result.stderr[-2000:]}")
    return out_path


def extract_video_id(link: str) -> str:
    """Pull the YouTube video id out of any link form Studio can produce:
    /shorts/<id>, /watch?v=<id>, youtu.be/<id>."""
    if not link:
        return ""
    m = re.search(r"(?:youtube\.com/(?:shorts/|watch\?v=)|youtu\.be/)([\w-]{6,})", link)
    if m:
        return m.group(1)
    # last-resort: bare trailing path segment
    return link.rstrip("/").split("/")[-1].split("?")[0]


def process_one(candidate, video_num: int, schedule_dt, batch_num: int,
                rows, by_id, channel: str = "chrome", headless: bool = False,
                yt_cookies: Path | None = None,
                fb_cookies: Path | None = None) -> tuple[str, int, object]:
    """Download -> edit -> upload+schedule a single candidate reel. Returns
    (status, video_num, schedule_dt) where status is 'uploaded', 'skipped'
    (too short to be worth uploading) or 'failed'. Raises nothing --
    failures are logged here and reflected in the return value."""
    reel_id = candidate["reel_id"]
    reel_url = candidate["reel_url"]
    video_title = f"Pure Talent {video_num}"  # YouTube title only -- gets the number
    series_name = "Pure Talent"  # description text + in-video overlay -- no number
    schedule_date = schedule_dt.date()
    schedule_time = schedule_dt.strftime("%H:%M")

    log(f"Processing {reel_url} -> '{video_title}' scheduled {schedule_dt}")

    downloaded_path = None
    edited_path = None
    try:
        downloaded_path = download_reel(reel_url, reel_id, fb_cookies)
        log(f"  downloaded: {downloaded_path}")

        duration = probe_duration(downloaded_path)
        if duration < MIN_REEL_SECONDS:
            row = by_id[reel_id]
            row["status"] = "skipped"
            save_rows(rows)
            log(f"  {duration:.1f}s is shorter than the {MIN_REEL_SECONDS}s minimum -- "
                f"skipping this reel entirely (marked 'skipped', not retried).")
            return "skipped", video_num, schedule_dt

        trim_start = 0.0
        if duration > MAX_SHORT_SECONDS:
            trim_start = duration - MAX_SHORT_SECONDS + 0.5
            log(f"  {duration:.0f}s exceeds the {MAX_SHORT_SECONDS}s Shorts limit -- "
                f"trimming {trim_start:.1f}s off the start to fit.")

        edited_path = EDITED / f"{reel_id}_edited.mp4"
        edit_video(downloaded_path, edited_path, series_name, trim_start=trim_start)
        log(f"  edited: {edited_path}")

        link = upload_and_schedule(
            edited_path, video_title, series_name, schedule_date, schedule_time,
            headless=headless, channel=channel, cookies_json=yt_cookies,
        )
        yt_id = extract_video_id(link)
        log(f"  uploaded+scheduled: {link or '(link not captured)'}")

        row = by_id[reel_id]
        row["status"] = "uploaded"
        row["video_name"] = video_title
        row["batch_number"] = str(batch_num)
        row["scheduled_date"] = schedule_dt.isoformat()
        row["youtube_video_id"] = yt_id
        save_rows(rows)

        return "uploaded", video_num + 1, schedule_dt + SCHEDULE_INTERVAL

    except Exception as exc:
        # The exception summary is logged on its own line first because
        # Playwright errors can carry megabytes of retry-log text -- a
        # plain tail slice of the full traceback can cut off the actual
        # exception message entirely, hiding what really happened.
        log(f"  FAILED for {reel_url}: {type(exc).__name__}: {exc}"[:1000])
        log(traceback.format_exc()[-3000:])

        if _is_permanent_failure(exc):
            row = by_id[reel_id]
            row["status"] = "failed"
            save_rows(rows)
            log("  this reel can't be extracted (deleted/private/unsupported) -- "
                "marking 'failed' so it stops being retried every batch.")
        else:
            log("  leaving as 'remaining' for retry, moving to next candidate.")
        return "failed", video_num, schedule_dt

    finally:
        for p in (downloaded_path, edited_path):
            try:
                if p and Path(p).exists():
                    Path(p).unlink()
            except Exception as cleanup_err:
                log(f"  cleanup warning for {p}: {cleanup_err}")


def main():
    lock = singleton_lock.acquire("pipeline")
    if lock is None:
        log("Another pipeline.py instance already holds the lock -- this machine is known to "
            "spontaneously duplicate process launches, so this is that redundant copy. Exiting "
            "immediately without touching data/reels.csv or the browser profiles.")
        return

    _start_watchdog()

    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--start-date", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--upload-time", type=str, default=DEFAULT_UPLOAD_TIME,
                         help="HH:MM (24h), interpreted in US Eastern Time")
    parser.add_argument("--max-runtime", type=int, default=0,
                        help="minutes; stop cleanly after this budget (cloud jobs: "
                             "5h wallclock cap -> use ~280 to leave margin)")
    parser.add_argument("--start-from-csv", action="store_true",
                        help="ignore --start-date/--upload-time; continue from the "
                             "last scheduled_date in the CSV + SCHEDULE_INTERVAL")
    parser.add_argument("--channel", default="chrome",
                        help="chrome (local) or chromium (cloud runner)")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--yt-cookies", type=str, default=None,
                        help="Playwright-format cookie JSON (cloud mode)")
    parser.add_argument("--fb-cookies", type=str, default=None,
                        help="Playwright-format cookie JSON (cloud mode)")
    args = parser.parse_args()

    if args.start_from_csv:
        rows0 = load_rows()
        last = max((dt.datetime.fromisoformat(r["scheduled_date"])
                    for r in rows0 if r.get("scheduled_date")), default=None)
        if last is None:
            log("--start-from-csv but no scheduled_date in CSV -- use --start-date.")
            return
        schedule_dt = last + SCHEDULE_INTERVAL
        log(f"Continuing schedule from CSV: last={last} next={schedule_dt}")
    else:
        if not args.start_date:
            log("Either --start-date or --start-from-csv is required.")
            return
        schedule_dt = dt.datetime.strptime(
            f"{args.start_date} {args.upload_time}", "%Y-%m-%d %H:%M"
        )

    runtime_deadline = None
    if args.max_runtime:
        runtime_deadline = time.monotonic() + args.max_runtime * 60
        log(f"Runtime budget: {args.max_runtime} minutes (deadline set).")

    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    EDITED.mkdir(parents=True, exist_ok=True)
    _cleanup_orphaned_files()

    rows = load_rows()
    by_id = {r["reel_id"]: r for r in rows}

    remaining = [r for r in rows if r["status"] == "remaining"]
    remaining.sort(key=lambda r: int(r["remaining_order"]) if r["remaining_order"] else 0)

    if not remaining:
        log("No remaining reels to process. Exiting.")
        return

    target = args.batch_size
    max_attempts = target * MAX_ATTEMPT_MULTIPLIER
    log(f"Starting batch: target={target} first_schedule={schedule_dt} "
        f"interval={SCHEDULE_INTERVAL} candidates_available={len(remaining)}")

    succeeded = 0
    attempted = 0
    consecutive_failures = 0

    video_num = next_video_number(rows)
    batch_num = next_batch_number(rows)

    for candidate in remaining:
        if succeeded >= target or attempted >= max_attempts:
            break
        if runtime_deadline is not None and time.monotonic() >= runtime_deadline:
            log(f"Runtime budget exhausted ({args.max_runtime} min) -- stopping the batch "
                f"cleanly. Uploaded so far: {succeeded}. Remaining reels stay 'remaining' "
                f"for the next run.")
            break
        attempted += 1
        log(f"[{attempted}/{max_attempts}] Processing {candidate['reel_url']}")

        status, video_num, schedule_dt = process_one(
            candidate, video_num, schedule_dt, batch_num, rows, by_id,
            channel=args.channel, headless=args.headless,
            yt_cookies=Path(args.yt_cookies) if args.yt_cookies else None,
            fb_cookies=Path(args.fb_cookies) if args.fb_cookies else None,
        )
        if status == "uploaded":
            succeeded += 1
            consecutive_failures = 0
        elif status == "skipped":
            consecutive_failures = 0  # a skip is not a failure
        else:
            consecutive_failures += 1
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                log(f"WARNING: {consecutive_failures} failures in a row -- stopping this batch early "
                    f"instead of burning through the rest of the candidates. This usually means something "
                    f"structural broke (YouTube upload limit/verification prompt, expired login session, a "
                    f"Studio UI change) rather than a problem with individual reels. Check the error above "
                    f"(and any screenshot path it mentions) before starting another batch.")
                break
        if runtime_deadline is not None and time.monotonic() >= runtime_deadline:
            log(f"Runtime budget exhausted ({args.max_runtime} min) -- stopping the batch "
                f"cleanly. Uploaded so far: {succeeded}.")
            break

    log(f"Batch finished. succeeded={succeeded} attempted={attempted} target={target}")
    if succeeded < target:
        log(f"WARNING: only {succeeded}/{target} reels were successfully scheduled.")


if __name__ == "__main__":
    main()
