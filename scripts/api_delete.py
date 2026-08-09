"""Deletes flagged videos via the YouTube Data API (videos.delete).

The cloud claims run cannot scan Studio (cookie sessions are rejected on
fresh runners, and the Data API exposes no claim/notice data). Instead, the
LOCAL claims_checker -- which can read the Notices column -- appends every
newly found flagged video to data/flagged_videos.csv, and this script runs
in the cloud and deletes those videos by ID, then marks the matching rows
'deleted' in data/reels.csv.

Usage:
  python api_delete.py                          # delete everything in flagged_videos.csv
  python api_delete.py --ids id1,id2            # explicit list instead
  python api_delete.py --dry-run                # list only

Credentials from env: GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN.
"""
import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path

from api_uploader import _build_youtube

ROOT = Path(__file__).resolve().parent.parent
FLAGGED_PATH = ROOT / "data" / "flagged_videos.csv"
CSV_PATH = ROOT / "data" / "reels.csv"

FLAGGED_FIELDS = ["video_id", "title", "notice", "detected_at"]


def log(msg: str):
    line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


def load_flagged():
    if not FLAGGED_PATH.exists():
        return []
    with open(FLAGGED_PATH, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("video_id")]


def save_flagged(rows):
    with open(FLAGGED_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FLAGGED_FIELDS)
        w.writeheader()
        w.writerows(rows)


def load_rows():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_rows(rows):
    if not rows:
        return
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def mark_csv_deleted(video_id: str):
    """Mark the reels.csv row whose youtube_video_id matches as 'deleted'."""
    rows = load_rows()
    updated = 0
    for r in rows:
        if r.get("youtube_video_id") == video_id and r.get("status") != "deleted":
            r["status"] = "deleted"
            updated += 1
    if updated:
        save_rows(rows)
        log(f"  reels.csv: marked {updated} row(s) 'deleted'.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", type=str, default="",
                    help="comma-separated video IDs (default: flagged_videos.csv)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.ids:
        entries = [{"video_id": v.strip(), "title": "", "notice": "", "detected_at": ""}
                   for v in args.ids.split(",") if v.strip()]
        flagged = []
    else:
        flagged = load_flagged()
        entries = flagged

    if not entries:
        log("No flagged videos to delete.")
        return

    log(f"Deleting {len(entries)} video(s) via API...")
    youtube = _build_youtube()
    remaining = []
    for e in entries:
        vid = e["video_id"]
        label = e.get("title") or vid
        try:
            if args.dry_run:
                log(f"  [dry-run] would delete {label} ({vid})")
                continue
            youtube.videos().delete(id=vid).execute()
            log(f"  DELETED {label} ({vid})")
            mark_csv_deleted(vid)
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 404:
                log(f"  {label} ({vid}) already gone (404) -- marking handled.")
                mark_csv_deleted(vid)
            else:
                log(f"  delete FAILED for {label} ({vid}): {exc}")
                remaining.append(e)
    if not args.dry_run:
        if flagged:
            save_flagged(remaining)
            log(f"flagged_videos.csv: {len(remaining)} still pending.")


if __name__ == "__main__":
    main()
