"""Cloud worker entrypoint (repo B). Reads secrets from env, writes them to
local files, then dispatches to pipeline.py (upload run) or claims_checker.py
(claims run). Called by the GitHub Actions workflows."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repo root
sys.path.insert(0, str(ROOT / "scripts"))

RUN_TYPE = os.environ.get("RUN_TYPE", "").strip()
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

YT_COOKIES = DATA_DIR / "youtube_cookies.json"
FB_COOKIES = DATA_DIR / "facebook_cookies.json"


def write_secret_cookies():
    yt = os.environ.get("YT_COOKIES")
    fb = os.environ.get("FB_COOKIES")
    if not yt or not fb:
        raise SystemExit("Missing YT_COOKIES or FB_COOKIES secret in environment")
    YT_COOKIES.write_text(yt)
    FB_COOKIES.write_text(fb)


def run(cmd: list[str]) -> int:
    print(f"[worker] running: {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd)


def main():
    write_secret_cookies()
    py = sys.executable
    api_available = bool(os.environ.get("GOOGLE_REFRESH_TOKEN") and
                         os.environ.get("GOOGLE_CLIENT_SECRET"))
    if RUN_TYPE == "upload":
        args = [
            str(py), str(ROOT / "scripts" / "pipeline.py"),
            "--batch-size", os.environ.get("BATCH_SIZE", "10"),
            "--start-from-csv",
            "--max-runtime", os.environ.get("MAX_RUNTIME", "270"),
            "--channel", "chromium",
            "--yt-cookies", str(YT_COOKIES),
            "--fb-cookies", str(FB_COOKIES),
        ]
        if api_available:
            args += ["--yt-upload-mode", "api"]
            print("[worker] using YouTube Data API uploader (OAuth token present)",
                  flush=True)
        else:
            print("[worker] no OAuth token -- falling back to browser upload "
                  "(expected to fail from GitHub IPs)", flush=True)
    elif RUN_TYPE == "claims":
        if api_available:
            args = [
                str(py), str(ROOT / "scripts" / "api_delete.py"),
            ]
            print("[worker] claims run via YouTube Data API (flagged_videos.csv)",
                  flush=True)
        else:
            args = [
                str(py), str(ROOT / "scripts" / "claims_checker.py"),
                "--delete",
                "--cookies", str(YT_COOKIES),
            ]
            print("[worker] no OAuth token -- falling back to browser claims "
                  "checker (expected to fail from GitHub IPs)", flush=True)
    else:
        raise SystemExit(f"Unknown RUN_TYPE: {RUN_TYPE!r}")
    code = run(args)
    if code != 0:
        print(f"[worker] {RUN_TYPE} run failed with exit code {code}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
