"""Studio-native copyright claims checker.

Scans BOTH the Videos and Shorts tabs of YouTube Studio's channel-content
page (paginated, 30 rows/page). For every video whose Notices column
(tablecell-restrictions) contains ANY notice text -- copyright claims,
earning limitations, strikes, etc. -- it deletes the video forever and marks
the row 'deleted' in data/reels.csv.

Deletion flow (derived from live DOM probing, Aug 2026):
  hover row -> click ytcp-icon-button.open-menu-button ("Options")
  -> menu item "Delete forever"
  -> dialog "Permanently delete this video?" -> tick the "I understand"
     checkbox -> click the dialog's "Delete forever" button.

Usage:
  python claims_checker.py --dry-run     # list what WOULD be deleted
  python claims_checker.py --delete      # actually delete
  python claims_checker.py --cookies cookies.json [--delete]  # cloud mode:
                                         # fresh profile + imported cookies,
                                         # headless (Google blocks headless
                                         # CDP sign-ins, so cloud jobs must
                                         # run headed on Windows runners)
"""
import argparse
import csv
import datetime as dt
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
YT_PROFILE = ROOT / "automation_profiles" / "youtube_profile"
CSV_PATH = ROOT / "data" / "reels.csv"
CHANNEL_ID = "UCoGgFOYfAYqvSWkctlEWKcA"

TABS = [
    ("videos", f"https://studio.youtube.com/channel/{CHANNEL_ID}/videos"),
    ("shorts", f"https://studio.youtube.com/channel/{CHANNEL_ID}/videos/short"),
]

EMPTY_GLYPHS = {"—", "\uFFFD", "-", ""}


def log(msg: str):
    line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("cp1252", errors="replace").decode("cp1252"), flush=True)


def _diag(page, label, text_extra=""):
    diag_dir = ROOT / "logs"
    diag_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    shot = diag_dir / f"claims_{label}_{stamp}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
    except Exception as exc:
        shot = f"(capture failed: {exc})"
    diag = f"{label}: screenshot {shot}"
    try:
        diag += "\n" + page.inner_text("body")[:2000]
    except Exception:
        pass
    diag += text_extra
    return diag


def _load_rows():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _save_rows(rows):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def wait_for_list(page, label, timeout_ms=45000):
    """Wait until the list has rendered rows AND the footer shows totals
    ('... of N'). Studio's virtualized list can render only a partial first
    chunk if the scan starts too early."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            nrows = page.locator("ytcp-video-row").count()
            footer = page.locator("ytcp-table-footer").inner_text() or ""
            if nrows > 0 and "of" in footer:
                return nrows, footer.strip().replace("\n", " ")
        except Exception:
            pass
        page.wait_for_timeout(1500)
    log(_diag(page, f"list_not_loaded_{label}"))
    raise TimeoutError(f"list {label} did not load within {timeout_ms}ms")


def scan_page_rows(page):
    """Return [{video_id, title, notice}] for every row currently rendered.
    Uses short per-row timeouts -- virtualized lists may have unloaded rows;
    those are skipped with a note instead of stalling for 30s each."""
    out = []
    rows = page.locator("ytcp-video-row")
    for i in range(rows.count()):
        try:
            row = rows.nth(i)
            title_a = row.locator("a#video-title").first
            href = title_a.get_attribute("href", timeout=2000) or ""
            m = re.search(r"/video/([\w-]{6,})/", href)
            video_id = m.group(1) if m else ""
            title = (title_a.get_attribute("aria-label", timeout=2000)
                     or "").strip()
            notice_cell = row.locator(".tablecell-restrictions").first
            notice_text = (notice_cell.inner_text(timeout=2000) or "").strip()
            if notice_text in EMPTY_GLYPHS:
                notice_text = ""
            out.append({"video_id": video_id, "title": title, "notice": notice_text})
        except Exception:
            out.append({"video_id": "", "title": f"(row {i} unloaded)",
                        "notice": ""})
    return out


def _footer_range(page):
    """Current page's start offset from the footer 'X-Y of Z', or None.
    The range separator is an en-dash (\\u2013); the footer also contains the
    'Rows per page: 30' prefix, so the regex is anchored to the trailing
    'of Z' to grab the real range."""
    try:
        foot = page.locator("ytcp-table-footer").inner_text(timeout=3000) or ""
        m = re.search(r"(\d+)[\s\u2013\u2014-]+\d+\s+of\s+\d+$", foot)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _next_button(page):
    """The list footer's next-page control, or None (disabled/absent)."""
    for label in ("Navigate to the next page", "Next page", "Next"):
        btn = page.locator(f"ytcp-icon-button[tooltip-label='{label}'], "
                           f"ytcp-icon-button[aria-label='{label}'], "
                           f"ytcp-icon-button[aria-label*='{label}']").first
        try:
            if btn.is_visible(timeout=1500) and btn.is_enabled():
                return btn
        except Exception:
            pass
    return None


def delete_video(page, video_id: str, title: str) -> bool:
    """Hover the row -> Options -> Delete forever -> confirm. Returns True on
    success, False (with diagnostics logged) on failure."""
    try:
        row = page.locator(f"ytcp-video-row:has(a[href*='{video_id}'])").first
        row.scroll_into_view_if_needed(timeout=6000)
        row.hover()
        page.wait_for_timeout(800)
        row.locator("ytcp-icon-button.open-menu-button").first.click(timeout=8000)
        page.wait_for_timeout(1200)
        page.get_by_text("Delete forever", exact=False).first.click(timeout=8000)
        page.wait_for_timeout(2000)
        dlg = page.locator("tp-yt-paper-dialog:visible, ytcp-dialog:visible").last
        dlg.wait_for(state="visible", timeout=10000)
        dtext = dlg.inner_text() or ""
        if "Permanently delete" not in dtext:
            log(f"    unexpected dialog for {title}: {dtext[:200]}")
        checkbox = dlg.locator("[role=checkbox]").first
        try:
            if checkbox.get_attribute("aria-checked") != "true":
                checkbox.click(timeout=5000)
                page.wait_for_timeout(400)
        except Exception as exc:
            log(f"    checkbox click failed ({exc}) -- continuing to confirm")
        confirm = dlg.get_by_role("button", name=re.compile("Delete forever", re.I)).first
        confirm.click(timeout=8000)
        page.wait_for_timeout(3500)
        return True
    except Exception as exc:
        log(f"    DELETE FAILED for {title} ({video_id}): {exc}")
        log(_diag(page, f"delete_fail_{title}"))
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


TRANSIENT_NOTICE_PREFIXES = ("Checks",)  # e.g. "Checks starting soon" = status, not a violation
EXCLUDED_VIDEO_IDS = {"jbdk4J2Xc44"}  # "Teeth Whitening Powder..." -- not ours, keep
DELETE_TITLE_PATTERN = re.compile(r"^Pure Talent \d+")  # only our pipeline videos get deleted


def scan_and_delete(page, tab_name: str, do_delete: bool, max_pages: int,
                    seen_ids: set, found_all: list, excluded: set):
    """Scan one tab page by page. Videos already handled (seen_ids) are
    reported but skipped for deletion. Returns (found, deleted) lists."""
    found, deleted = [], []
    for page_no in range(1, max_pages + 1):
        try:
            nrows, footer = wait_for_list(page, tab_name)
        except TimeoutError:
            log(f"  [{tab_name}] list did not load -- moving on.")
            break
        log(f"  [{tab_name}] page {page_no}: {nrows} rows ({footer})")
        rows = scan_page_rows(page)
        for r in rows:
            if not r["notice"]:
                continue
            if r["notice"].startswith(TRANSIENT_NOTICE_PREFIXES):
                log(f"    transient notice (skipped) -> {r['title']!r}: {r['notice']}")
                continue
            if r["video_id"] in excluded:
                log(f"    EXCLUDED (kept) -> {r['title']!r} ({r['video_id']}): {r['notice']}")
                continue
            if not DELETE_TITLE_PATTERN.match(r["title"]):
                log(f"    NOT OUR VIDEO (kept) -> {r['title']!r}: {r['notice']}")
                continue
            dup = r["video_id"] in seen_ids
            log(f"    NOTICE -> {r['title']!r} ({r['video_id']}): {r['notice']}"
                + (" [already handled]" if dup else ""))
            if not dup and r["video_id"]:
                seen_ids.add(r["video_id"])
                found.append(r)
                found_all.append(r)
            if do_delete and not dup and r["video_id"]:
                ok = delete_video(page, r["video_id"], r["title"])
                deleted.append((r["title"], ok))
                if ok:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(5000)
        btn = _next_button(page)
        if btn is None:
            log(f"  [{tab_name}] no next page -- done.")
            break
        if page_no >= max_pages:
            log(f"  [{tab_name}] max_pages reached.")
            break
        # click next and wait for the footer range to advance (retry once)
        cur_start = _footer_range(page)
        stopped = False
        for attempt in range(2):
            btn = _next_button(page)
            if btn is None:
                log(f"  [{tab_name}] no next page -- done.")
                stopped = True
                break
            try:
                btn.click(timeout=8000)
            except Exception as exc:
                log(f"  [{tab_name}] next-page click failed: {exc}")
                log(_diag(page, "nextpage_fail"))
                stopped = True
                break
            if cur_start is not None:
                deadline = time.time() + 15
                advanced = False
                while time.time() < deadline:
                    if _footer_range(page) not in (None, cur_start):
                        advanced = True
                        break
                    page.wait_for_timeout(1000)
                if advanced:
                    break
                log(f"  [{tab_name}] list did not advance after click "
                    f"(attempt {attempt + 1}) -- retrying")
        else:
            log(f"  [{tab_name}] list would not advance -- moving on")
        if stopped:
            break
    return found, deleted


def update_csv(deleted_titles):
    rows = _load_rows()
    updated = 0
    for r in rows:
        name = r.get("video_name", "")
        num_m = re.search(r"(\d+)$", name)
        if not num_m:
            continue
        num = int(num_m.group(1))
        if any(re.search(r"Pure Talent (\d+)", t) and int(re.search(r"Pure Talent (\d+)", t).group(1)) == num
               for t in deleted_titles):
            if r.get("status") != "deleted":
                r["status"] = "deleted"
                updated += 1
    _save_rows(rows)
    log(f"CSV: marked {updated} row(s) 'deleted'.")


def run(do_delete: bool, cookies_file: Path | None, max_pages: int,
        extra_excluded: set):
    rows_backup = _load_rows()
    excluded = EXCLUDED_VIDEO_IDS | extra_excluded
    with sync_playwright() as p:
        if cookies_file:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(ROOT / "cloud_profile" / "yt"),
                channel="chrome", headless=False,
                viewport={"width": 1440, "height": 1000})
        else:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(YT_PROFILE), channel="chrome", headless=False,
                viewport={"width": 1440, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if cookies_file:
            cookies = [c for c in json.loads(Path(cookies_file).read_text())
                       if any(d in c.get("domain", "") for d in
                              (".youtube.com", "youtube.com", ".google.com", "google.com"))]
            ctx.add_cookies(cookies)
        all_found, all_deleted = [], []
        seen_ids = set()
        try:
            for tab_name, url in TABS:
                log(f"== Scanning {tab_name} tab ==")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(8000)
                body = page.inner_text("body")
                if "Sign in" in body and "Channel content" not in body:
                    log(f"NOT LOGGED IN on {tab_name} tab -- aborting.")
                    return
                found, deleted = scan_and_delete(page, tab_name, do_delete,
                                                 max_pages, seen_ids, all_found,
                                                 excluded)
                all_deleted += deleted
        finally:
            ctx.close()

    log(f"\n=== SUMMARY: {len(all_found)} video(s) with notices ===")
    for r in all_found:
        log(f"  - {r['title']} ({r['video_id']}): {r['notice']}")
    if do_delete:
        for title, ok in all_deleted:
            log(f"  delete {title}: {'OK' if ok else 'FAILED'}")
        update_csv([t for t, ok in all_deleted if ok])
    else:
        log("(dry-run -- nothing was deleted; pass --delete to remove them)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true",
                    help="actually delete noticed videos (default: dry-run)")
    ap.add_argument("--cookies", type=str, default=None,
                    help="JSON cookies file to import (cloud mode)")
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--exclude", type=str, default="",
                    help="comma-separated extra video IDs to never delete")
    args = ap.parse_args()
    run(do_delete=args.delete,
        cookies_file=Path(args.cookies) if args.cookies else None,
        max_pages=args.max_pages,
        extra_excluded=set(x.strip() for x in args.exclude.split(",") if x.strip()))


if __name__ == "__main__":
    main()
