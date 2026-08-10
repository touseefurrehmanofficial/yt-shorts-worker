"""Uploads an edited video to YouTube via the Data API v3 and schedules it.

Used by the cloud worker as the primary upload path (OAuth refresh token
works from any IP -- unlike cookie-imported browser sessions, which Google
rejects on fresh runners). Local runs still use the Studio browser path.

Credentials come from env (set by the GitHub Actions workflow):
  GOOGLE_CLIENT_SECRET   -- the OAuth client_secret.json content (Desktop app)
  GOOGLE_REFRESH_TOKEN   -- the token.json content (refresh_token granted
                            once, locally, with the user's Google account)

Scheduling: the video is uploaded as privacyStatus=private with publishAt
set; YouTube makes it public automatically at publishAt. publishAt must be
ISO-8601 WITH timezone; the pipeline schedules in US Eastern wall-clock, so
the naive datetime is converted to UTC via America/New_York.
"""
import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube"]
CATEGORY_ID = "22"  # People & Blogs


def _load_credentials():
    """Credentials from env (cloud runner) or from local files (this machine)."""
    client_env = os.environ.get("GOOGLE_CLIENT_SECRET")
    token_env = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if client_env and token_env:
        return json.loads(client_env), json.loads(token_env)

    root = Path(__file__).resolve().parent.parent
    token_path = root / "data" / "yt_oauth_token.json"
    candidates = [
        *(root / "data").glob("client_secret_*.json"),
        *(Path.home() / "Downloads").glob("client_secret_*.json"),
    ]
    if token_path.exists() and candidates:
        return json.loads(candidates[0].read_text()), json.loads(token_path.read_text())
    raise RuntimeError(
        "No Google credentials: set GOOGLE_CLIENT_SECRET/GOOGLE_REFRESH_TOKEN env "
        "or have data/yt_oauth_token.json + a client_secret_*.json file present"
    )


def _build_youtube():
    client, token = _load_credentials()
    if "refresh_token" not in token:
        raise RuntimeError("GOOGLE_REFRESH_TOKEN has no refresh_token field")
    creds = Credentials(
        token=None,
        refresh_token=token["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client.get("installed", client).get("client_id"),
        client_secret=client.get("installed", client).get("client_secret"),
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_and_schedule_api(video_path: Path, title: str, description: str,
                            schedule_dt: dt.datetime) -> str:
    """Upload video_path scheduled public at schedule_dt (naive, US Eastern).
    Returns the youtube.com/shorts/<id> link."""
    video_path = Path(video_path)
    eastern = ZoneInfo("America/New_York")
    publish_at = schedule_dt.replace(tzinfo=eastern).astimezone(dt.timezone.utc)
    iso = publish_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    youtube = _build_youtube()
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": CATEGORY_ID,
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": iso,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024,
                            resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body,
                                      media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"[api_uploader] {status.progress() * 100:.0f}% uploaded",
                  flush=True)
    video_id = response.get("id", "")
    print(f"[api_uploader] scheduled {title} -> {video_id} at {iso}",
          flush=True)
    return f"https://www.youtube.com/shorts/{video_id}"
