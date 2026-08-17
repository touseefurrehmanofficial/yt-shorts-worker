"""Gemini-generated title + description for edited reels.

Called by pipeline.py right before each upload. Uploads the edited video to
the Gemini Files API, asks a vision-capable model to describe the actual
content, and returns JSON {"title": ..., "description": ...}.

Degrades gracefully: no API key, missing package or any API error ->
(None, None), and the pipeline falls back to the stock 'Pure Talent N'
title. The key is read from GEMINI_API_KEY (env) or .env at the project
root.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

ENABLED = True

MODELS = ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-2.5-flash")

PROMPT = (
    "You write titles and descriptions for a YouTube Shorts channel that "
    "posts raw, viral-style performance clips (music, dance, singing, "
    "showmanship).\n"
    "Watch the attached video, then output ONLY one JSON object - no "
    "markdown fences, no commentary:\n"
    '{"title": "...", "description": "..."}\n'
    "Rules:\n"
    "- title: max 80 characters, hook-style and descriptive of THIS video's "
    "actual content. No clickbait lies, no quotes, no hashtags, no number "
    "suffixes.\n"
    "- description: 3-5 sentences describing what happens in the video and "
    "engaging the viewer, then a short hashtag line. Max 900 characters."
)


def _load_key() -> str:
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if key:
        return key
    env_path = ROOT / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("GEMINI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return ""


def generate(video_path, timeout_seconds: int = 240):
    """Return (title, description) for the video, or (None, None)."""
    try:
        from google import genai
    except Exception as exc:
        print(f"[gemini_meta] google-genai unavailable: {exc}", flush=True)
        return None, None

    key = _load_key()
    if not key:
        print("[gemini_meta] no GEMINI_API_KEY - falling back to stock title.",
              flush=True)
        return None, None

    client = genai.Client(api_key=key,
                          http_options={"timeout": timeout_seconds * 1000})
    uploaded = None
    try:
        uploaded = client.files.upload(file=str(video_path))
        deadline = time.time() + 180
        while True:
            state = getattr(client.files.get(name=uploaded.name).state,
                            "name", "")
            if state == "ACTIVE":
                break
            if state == "FAILED":
                print("[gemini_meta] file processing failed.", flush=True)
                return None, None
            if time.time() > deadline:
                print("[gemini_meta] file did not become ACTIVE in time.",
                      flush=True)
                return None, None
            time.sleep(5)
        last_err = "no model responded"
        for model in MODELS:
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[uploaded, PROMPT],
                )
                text = (resp.text or "").strip()
                match = re.search(r"\{.*\}", text, re.S)
                data = json.loads(match.group(0)) if match else {}
                title = str(data.get("title", "")).strip()
                description = str(data.get("description", "")).strip()
                if title and description:
                    print(f"[gemini_meta] title/description generated "
                          f"({model}).", flush=True)
                    return title[:100], description[:4990]
                last_err = f"empty/invalid fields in: {text[:200]!r}"
            except Exception as exc:
                last_err = str(exc)
                transient = any(m in str(exc) for m in
                                ("503", "429", "500", "UNAVAILABLE",
                                 "RESOURCE_EXHAUSTED"))
                if not transient and "404" not in str(exc) \
                        and "not found" not in str(exc).lower():
                    print(f"[gemini_meta] {model} failed: {exc}", flush=True)
                    break
        print(f"[gemini_meta] generation failed: {last_err}", flush=True)
        return None, None
    except Exception as exc:
        print(f"[gemini_meta] upload/generation failed: {exc}", flush=True)
        return None, None
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass