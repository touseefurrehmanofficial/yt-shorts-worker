"""Uploads an edited video to YouTube Studio and schedules it, using the
logged-in youtube_profile automation Chrome profile. Selectors were derived
by interactively driving studio.youtube.com (see yt_explore.py).

Robustness notes (learned from production failures):
- Studio re-renders the schedule paper-inputs after the date picker closes,
  which detaches stale locators mid-click. Every schedule field is therefore
  re-queried by its CURRENT VALUE right before acting on it, and its new
  value is verified after Enter instead of trusting a fixed sleep.
- Every click that has ever failed intermittently captures a screenshot +
  visible page text so the log is self-diagnosing.
"""
import datetime as dt
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
YT_PROFILE = ROOT / "automation_profiles" / "youtube_profile"

DATE_VALUE_RE = re.compile(r"^\d{1,2} \w{3} \d{4}$")
TIME_VALUE_RE = re.compile(r"^\d{2}:\d{2}$")
VIDEO_ID_RE = re.compile(r"(?:youtube\.com/(?:shorts/|watch\?v=)|youtu\.be/)([\w-]{6,})")


def _format_studio_date(d: dt.date) -> str:
    return f"{d.day} {d.strftime('%b')} {d.year}"


def _wait_for_visible_text(page, text, timeout_ms=180000, poll_ms=2000):
    """Poll for an ACTUALLY VISIBLE element matching text, checking every
    match rather than trusting the first/only DOM match -- Studio sometimes
    has a same-text but permanently-hidden hover-tooltip variant that a plain
    locator.wait_for() would latch onto and block on forever."""
    locator = page.get_by_text(text, exact=False)
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        count = locator.count()
        for i in range(count):
            try:
                if locator.nth(i).is_visible():
                    return True
            except Exception:
                pass
        page.wait_for_timeout(poll_ms)

    diag_msg = f"No visible element matching text {text!r} within {timeout_ms}ms."
    try:
        diag_dir = ROOT / "logs"
        diag_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = diag_dir / f"upload_timeout_{stamp}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        visible_text = page.inner_text("body")[:1500]
        diag_msg += f" Screenshot: {screenshot_path}\nVisible page text at timeout:\n{visible_text}"
    except Exception as diag_err:
        diag_msg += f" (failed to capture diagnostics: {diag_err})"
    raise TimeoutError(diag_msg)


def _click_with_diagnostics(page, locator, label, timeout_ms=30000):
    """Click, but on failure capture a screenshot + visible page text before
    re-raising -- several scheduling-panel clicks have intermittently failed
    in production in ways that never reproduce in an isolated retest, so the
    only way to actually see what Studio was showing at that exact moment is
    to capture it live, in the batch run itself, rather than guessing."""
    try:
        locator.click(timeout=timeout_ms)
    except Exception as exc:
        diag_dir = ROOT / "logs"
        diag_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = diag_dir / f"click_fail_{label}_{stamp}.png"
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            screenshot_path = "(failed to capture)"
        try:
            visible_text = page.inner_text("body")[:2000]
        except Exception:
            visible_text = "(failed to capture page text)"
        raise TimeoutError(
            f"Click failed for {label!r}: {exc}\n"
            f"Screenshot: {screenshot_path}\nVisible page text at failure:\n{visible_text}"
        ) from exc


def _paper_inputs(page):
    return page.locator("tp-yt-paper-input input")


def _wait_for_backdrop_closed(page, timeout_ms=8000):
    """The shared Polymer overlay backdrop intercepts clicks until its close
    animation finishes -- wait for it to actually disappear rather than
    sleeping a fixed amount that is sometimes too short."""
    try:
        page.locator("tp-yt-iron-overlay-backdrop.opened").wait_for(state="hidden", timeout=timeout_ms)
    except Exception:
        pass
    page.wait_for_timeout(250)


def _find_paper_input(page, value_re, label, timeout_ms=12000):
    """Re-query the schedule paper-inputs fresh and return the (index,
    locator) whose current value matches value_re. Studio re-renders these
    inputs when pickers open/close, so a locator captured earlier can be
    detached; value-matching right before acting sidesteps both the ordering
    ambiguity (date vs time input) and the stale-detached problem."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        inputs = _paper_inputs(page)
        count = inputs.count()
        for i in range(count):
            try:
                if value_re.match((inputs.nth(i).input_value() or "").strip()):
                    return i, inputs.nth(i)
            except Exception:
                pass
        page.wait_for_timeout(300)
    raise TimeoutError(f"No schedule paper-input matching {value_re.pattern!r} ({label}) "
                       f"within {timeout_ms}ms.")


def _set_schedule_field(page, value, value_re, label):
    """Click the matching paper-input, replace its content with `value`,
    READ BACK the typed text (retyping once if it didn't land), confirm with
    Enter, then wait for the picker overlay to close. The read-back check
    matters: the time field's default is often "10:00" -- the same string we
    usually type -- so a failed keystroke would otherwise pass a naive
    post-commit comparison."""
    _, field = _find_paper_input(page, value_re, label)
    _click_with_diagnostics(page, field, f"{label}_field")
    page.wait_for_timeout(400)
    for attempt in range(2):
        page.keyboard.press("Control+a")
        page.keyboard.type(value)
        page.wait_for_timeout(400)
        try:
            typed_back = field.input_value()
        except Exception:
            typed_back = ""
        if (typed_back or "").strip() == value:
            break
        page.keyboard.press("Escape")  # reset any picker that opened mid-type
        page.wait_for_timeout(300)
    page.keyboard.press("Enter")
    _wait_for_backdrop_closed(page)
    if (typed_back or "").strip() != value:
        raise TimeoutError(
            f"{label} field did not accept {value!r} (read back {typed_back!r})."
        )
    return typed_back


def _extract_video_id(link: str) -> str:
    if not link:
        return ""
    m = VIDEO_ID_RE.search(link)
    return m.group(1) if m else ""


def upload_and_schedule(video_path: Path, title: str, description: str,
                         schedule_date: dt.date, schedule_time: str = "10:00",
                         headless: bool = False, channel: str = "chrome",
                         cookies_json: Path | None = None) -> str:
    """Uploads video_path, sets title/description, and schedules it to go
    public at schedule_date/schedule_time. Returns the youtube.com/shorts
    video link (or "" if Studio didn't expose it).

    channel="chrome" uses the installed Google Chrome (local runs);
    channel="chromium" uses Playwright's bundled browser (cloud runner).
    cookies_json: Playwright-format cookie JSON to import into a FRESH
    profile (cloud mode) instead of using the logged-in automation profile.
    Google blocks headless/CDP sign-ins, so cloud jobs must run headed
    (Windows runners support it)."""
    video_path = Path(video_path)

    with sync_playwright() as p:
        if cookies_json:
            user_data_dir = str(ROOT / "cloud_profile" / "yt")
        else:
            user_data_dir = str(YT_PROFILE)
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            channel=channel,
            headless=headless,
            viewport={"width": 1440, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if cookies_json:
            cookies = [c for c in json.loads(Path(cookies_json).read_text())
                       if any(d in c.get("domain", "") for d in
                              (".youtube.com", "youtube.com", ".google.com", "google.com"))]
            ctx.add_cookies(cookies)
        page.goto("https://studio.youtube.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        # --- open the upload dialog (retry once if it doesn't open) ---
        file_input = None
        for _ in range(2):
            page.get_by_label("Create", exact=True).click()
            page.wait_for_timeout(1200)
            page.get_by_text("Upload videos", exact=False).first.click()
            page.wait_for_timeout(2000)
            try:
                file_input = page.locator("input[type='file']").first
                file_input.wait_for(state="attached", timeout=10000)
                break
            except Exception:
                page.keyboard.press("Escape")
                page.wait_for_timeout(800)
        if file_input is None:
            raise TimeoutError("Upload dialog never appeared after two attempts.")

        file_input.set_input_files(str(video_path))
        page.wait_for_timeout(3000)

        # Wait for the actual file upload to finish before touching anything
        # else -- clicking Next / closing the browser too early interrupts
        # the upload and leaves an orphaned draft in Studio.
        _wait_for_visible_text(page, "Upload complete", timeout_ms=600000)
        page.wait_for_timeout(1500)

        # Title
        title_box = page.locator("#textbox").first
        title_box.click()
        page.keyboard.press("Control+a")
        page.keyboard.type(title)
        page.wait_for_timeout(400)

        # Description
        desc_box = page.locator("#textbox").nth(1)
        desc_box.click()
        page.keyboard.press("Control+a")
        page.keyboard.type(description)
        page.wait_for_timeout(400)

        # Details -> Video elements -> Checks -> Visibility
        for _ in range(3):
            page.get_by_role("button", name="Next", exact=True).click()
            page.wait_for_timeout(2000)

        # Expand the Schedule panel (anchor on the unique description text,
        # since a plain "Schedule" text match is ambiguous elsewhere in Studio)
        page.get_by_text("Select a date to make your video public.", exact=True).click()
        page.wait_for_timeout(1200)

        # The date field renders as a ytcp-dropdown-trigger (a text label, not
        # a real <input>) until you click it directly -- only then does it
        # turn into an editable tp-yt-paper-input we can type into.
        _click_with_diagnostics(page, page.locator("ytcp-dropdown-trigger").first,
                                "date_trigger")
        _wait_for_backdrop_closed(page)

        date_str = _format_studio_date(schedule_date)
        committed_date = _set_schedule_field(page, date_str, DATE_VALUE_RE, "date")
        if not DATE_VALUE_RE.match(committed_date or ""):
            raise TimeoutError(
                f"Date field did not commit {date_str!r} (committed={committed_date!r})."
            )

        committed_time = _set_schedule_field(page, schedule_time, TIME_VALUE_RE, "time")
        if not TIME_VALUE_RE.match(committed_time or ""):
            raise TimeoutError(
                f"Time field did not commit {schedule_time!r} (committed={committed_time!r})."
            )

        # Force the timezone to US Eastern so scheduled times are unambiguous
        # regardless of what locale this machine happens to be set to.
        _click_with_diagnostics(page, page.get_by_text("Time zone", exact=True), "timezone_trigger")
        page.wait_for_timeout(800)
        _click_with_diagnostics(page, page.get_by_text("New York", exact=False).first, "new_york_option")
        page.wait_for_timeout(600)

        # blur so both fields commit
        _click_with_diagnostics(page, page.get_by_text("Schedule as public", exact=True), "schedule_as_public")
        page.wait_for_timeout(500)

        schedule_btn = page.get_by_role("button", name="Schedule", exact=True)
        _click_with_diagnostics(page, schedule_btn, "schedule_button")
        page.wait_for_timeout(3000)

        # Capture the video link AFTER scheduling -- before the dialog closes,
        # the scheduled video's "Copy link" anchor is present; earlier in the
        # flow it usually isn't, which is why some rows have no video id.
        # Scope the query INSIDE the upload dialog: the dashboard page behind
        # it also contains watch?v= links to old videos, and a page-wide first
        # match grabs those stale ids (seen in a live probe run).
        video_link = ""
        link_scope = (
            page.locator(
                "ytcp-uploads-dialog a[href*='youtube.com/shorts/'], "
                "ytcp-uploads-dialog a[href*='youtube.com/watch'], "
                "ytcp-uploads-dialog a[href*='youtu.be/']"
            ).first
        )
        try:
            video_link = link_scope.get_attribute("href", timeout=3000) or ""
        except Exception:
            pass
        if not video_link:
            # Dialog already closed -- fall back to page-wide youtu.be/shorts
            # forms only (the dashboard's watch?v= links are never these).
            try:
                link_el = page.locator(
                    "a[href*='youtube.com/shorts/'], a[href*='youtu.be/']"
                ).first
                video_link = link_el.get_attribute("href", timeout=3000) or ""
            except Exception:
                pass
        if not video_link:
            # Last resort: the completion panel prints the youtu.be/ID link as
            # text even when no anchor matched -- read the dialog body.
            try:
                dialog_text = page.locator("ytcp-uploads-dialog").inner_text(timeout=3000)
            except Exception:
                dialog_text = ""
            m = VIDEO_ID_RE.search(dialog_text or "")
            if m:
                video_link = f"https://www.youtube.com/shorts/{m.group(1)}"
        if video_link and "youtube.com/watch?v=" not in video_link and "/shorts/" not in video_link:
            # Normalize youtu.be/ID -> canonical shorts link.
            vid = _extract_video_id(video_link)
            if vid:
                video_link = f"https://www.youtube.com/shorts/{vid}"

        try:
            page.get_by_role("button", name="Got it").click(timeout=4000)
        except Exception:
            pass
        page.wait_for_timeout(800)

        try:
            page.locator(
                "ytcp-icon-button#close-icon-button, ytcp-button#close-button"
            ).first.click(timeout=4000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        ctx.close()
        return video_link


if __name__ == "__main__":
    import sys
    test_video = ROOT / "logs" / "synth_edited.mp4"
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    link = upload_and_schedule(
        test_video, "Pure Talent 1 TEST", "Pure Talent 1 TEST", tomorrow, "10:00"
    )
    print("Scheduled video link:", link)
