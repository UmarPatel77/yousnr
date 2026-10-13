#!/usr/bin/env python3
"""
YouSNR (yousnr.com) — static YouTube-style transcript pages for GitHub Pages.

  python yousnr.py add <url-or-id> [more ...] [--lang en]   fetch from YouTube, save, build
  python yousnr.py paste <url-or-id> [file.txt]             build from a transcript copied off YouTube
                                                             (no file = read the clipboard)
  python yousnr.py build                                    rebuild every page from data/
  python yousnr.py serve [--port 8787]                      on-demand server: any video, fetched on first visit
  python yousnr.py remove <url-or-id> [more ...]            delete videos from the site
  python yousnr.py signin                                   sign YouSNR's browser in to YouTube (optional)
  python yousnr.py correct <video> [--full]                 fix likely mistakes with speech recognition
  python yousnr.py usage | bakeoff <video>                  spending this month | compare the engines

Data lives in data/<ID>.json; the site is generated into docs/ (GitHub Pages source).
Run `add` from your own computer: YouTube blocks most cloud/CI IPs, including GitHub Actions.
"""
import argparse, datetime, html, json, os, re, subprocess, sys, threading, time, urllib.parse, urllib.request
from pathlib import Path

try:
    import correct as acc                      # accuracy layer (correct.py next to this file)
except Exception:                              # pragma: no cover
    acc = None

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
TEMPLATES = ROOT / "templates"
SITE = "https://yousnr.com"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


# ---------------------------------------------------------------- helpers

def video_id(s):
    """Accept a bare ID or any common YouTube / yousnr / yoursnr URL form."""
    s = s.strip()
    if ID_RE.match(s):
        return s
    u = urllib.parse.urlparse(s if "://" in s else "https://" + s)
    q = urllib.parse.parse_qs(u.query).get("v")
    if q and ID_RE.match(q[0]):
        return q[0]
    for part in reversed([p for p in u.path.split("/") if p]):
        if ID_RE.match(part):
            return part
    sys.exit(f"Can't find an 11-character video ID in: {s}")


def ts(seconds):
    """YouTube transcript-panel style: floored, m:ss under an hour, h:mm:ss after."""
    t = int(seconds)
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_ts(s):
    parts = [int(p) for p in s.split(":")]
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def chapters_from_description(desc):
    """YouTube's own rules: first stamp is 0:00, at least 3, each at least 10s long."""
    out = []
    for line in (desc or "").splitlines():
        m = re.match(r"^\s*[\[(]?((?:\d+:)?\d{1,2}:\d{2})[\])]?\s*[-–—:|.]?\s*(.+?)\s*$", line)
        if m:
            out.append({"start": parse_ts(m.group(1)), "title": m.group(2)})
    if len(out) < 3 or out[0]["start"] != 0:
        return []
    starts = [c["start"] for c in out]
    if any(b - a < 10 for a, b in zip(starts, starts[1:])):
        return []
    return out


# ---------------------------------------------------------------- fetching

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


def _json_after(page, marker):
    i = page.find(marker)
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(page, page.index("{", i))[0]
    except ValueError:
        return None


def fetch_metadata(vid):
    """Title, channel, date, chapters. One plain request to the watch page; yt-dlp only if that fails."""
    meta = {"id": vid, "title": "", "channel": "", "published": "", "description": "", "chapters": []}
    try:
        import requests
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        sess.cookies.set("SOCS", "CAI", domain=".youtube.com")
        page = sess.get(f"https://www.youtube.com/watch?v={vid}&hl=en", timeout=20).text
        pr = _json_after(page, "ytInitialPlayerResponse = ") or {}
        vd = pr.get("videoDetails") or {}
        if vd.get("title"):
            mf = (pr.get("microformat") or {}).get("playerMicroformatRenderer") or {}
            meta.update(title=vd["title"], channel=vd.get("author", ""),
                        published=(mf.get("publishDate") or mf.get("uploadDate") or "")[:10].replace("-", ""),
                        description=vd.get("shortDescription", ""), duration=int(vd.get("lengthSeconds") or 0),
                        category=mf.get("category", ""), music_type=vd.get("musicVideoType", ""))
            meta["chapters"] = chapters_from_description(meta["description"])
            return meta
    except Exception:
        pass
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        meta.update(
            title=info.get("title") or "",
            channel=info.get("channel") or info.get("uploader") or "",
            published=info.get("upload_date") or "",
            description=info.get("description") or "",
            duration=info.get("duration"),
            chapters=[{"start": int(c["start_time"]), "title": c["title"]} for c in info.get("chapters") or []],
            category=(info.get("categories") or [""])[0],
            music_type="MUSIC_VIDEO_TYPE" if info.get("track") and info.get("artist") else "",
        )
    except Exception as e:  # last resort: oEmbed gives title + channel
        print(f"  metadata via yt-dlp failed ({e.__class__.__name__}); trying oEmbed", file=sys.stderr)
        try:
            url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
                f"https://www.youtube.com/watch?v={vid}")
            with urllib.request.urlopen(url, timeout=15) as r:
                o = json.load(r)
            meta.update(title=o.get("title", ""), channel=o.get("author_name", ""))
        except Exception as e2:
            print(f"  oEmbed failed too ({e2.__class__.__name__})", file=sys.stderr)
    if not meta["chapters"]:
        meta["chapters"] = chapters_from_description(meta["description"])
    return meta


DIRECT_BLOCKED_UNTIL = 0.0


class DirectDownloadPaused(Exception):
    pass


class MusicVideo(Exception):
    pass


def is_music(meta):
    """YouTube's own music flag (official music videos, songs/'Art Tracks'), or the uploader's 'Music' category."""
    return bool(meta.get("music_type")) or (meta.get("category") or "").lower() == "music" \
        or (meta.get("channel") or "").endswith(" - Topic")


def get_transcript(vid, lang="en", browser=True, show=False, player=False, allow_music=False):
    """Fetch one video end to end and save it. Returns the saved record."""
    meta = fetch_metadata(vid)
    if not allow_music and is_music(meta):
        if meta.get("music_type"):
            why = f"YouTube flags it as {meta['music_type']}"
        elif (meta.get("category") or "").lower() == "music":
            why = "category: Music"
        else:
            why = "a '- Topic' music channel"
        raise MusicVideo(f"music video, skipped ({why})")
    chapters, segs, track = [], None, None
    if browser:
        for attempt in ((False,) if show else (True, False)):
            try:
                segs, chapters, track, how = fetch_browser(vid, show=show, offscreen=attempt)
                print(f"  transcript: {how}, {len(segs)} lines (exactly YouTube's transcript panel)")
                break
            except ImportError:
                print("  (the transcript-panel method needs:  python -m pip install --user playwright)")
                break
            except Exception as e:
                print(f"  transcript panel {'(off-screen window) ' if attempt else ''}failed: "
                      f"{e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}")
        if not segs:
            print("  falling back to the caption file")
    if not segs:
        global DIRECT_BLOCKED_UNTIL
        try:
            if player:
                raise DirectDownloadPaused("using the video player (--player)")
            if time.time() < DIRECT_BLOCKED_UNTIL:
                raise DirectDownloadPaused("direct downloads paused after YouTube refused one")
            segs, track = fetch_captions_fallback(vid, [lang, "en"])
            print(f"  transcript: caption file grouped like YouTube's panel ({len(segs)} lines)")
        except Exception as e:
            name = e.__class__.__name__
            if name in NO_TRANSCRIPT or name in UNAVAILABLE:
                raise
            # Usually YouTube refusing scripted caption downloads (429). A real browser playing the
            # video still gets them, so ask the video player for the captions instead.
            if name != "DirectDownloadPaused":
                DIRECT_BLOCKED_UNTIL = time.time() + 3600
                print(f"  direct caption download refused ({name}); using the video player for the next hour")
            try:
                segs, track = fetch_player_captions(vid, show=show)
            except ImportError:
                print("  (the player method needs:  python -m pip install --user playwright)")
                raise e
            except Exception as e2:
                if e2.__class__.__name__ in NO_TRANSCRIPT:
                    raise
                print(f"  player method failed too: {e2.__class__.__name__}: {str(e2).splitlines()[0] if str(e2) else ''}")
                raise RuntimeError("429 Too Many Requests (direct and player both refused)") if name == "DirectDownloadPaused" else e
            print(f"  transcript: video player captions grouped like YouTube's panel ({len(segs)} lines)")
    d = meta
    d["description"] = (d.get("description") or "")[:5000]
    if chapters:
        d["chapters"] = chapters
    d.update(segments=segs, track=track, source="youtube")
    save(d)
    write_page(d)
    write_indexes()
    return d


# ---- YouTube's own transcript panel ("Show transcript") -------------------
# The panel doesn't show the caption file. It asks YouTube for a pre-grouped transcript
# (~6 s per line for auto captions, YouTube's own timestamp labels, chapter headings in
# between). Since 2026 YouTube only answers that request for a real browser
# (FAILED_PRECONDITION otherwise), so we open the video in a hidden Chrome/Edge,
# press "Show transcript", and read the browser's own copy of the answer.

DEBUG_DIR = None
BASE = os.environ.get("YOUSNR_YT_BASE", "https://www.youtube.com")   # overridable for tests
UI_LABELS = {"in this video", "chapters", "transcript", "show transcript", "search transcript",
             "search in video", "key concepts", "sync to video time"}


def _dump(name, obj):
    if DEBUG_DIR:
        DEBUG_DIR.mkdir(exist_ok=True)
        data = obj if isinstance(obj, (str, bytes)) else json.dumps(obj, indent=1)
        (DEBUG_DIR / name).write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _walk(o):
    """Yield (key, value) for every dict entry, at any depth."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _text(o):
    if o is None:
        return ""
    if isinstance(o, str):
        return o
    if not isinstance(o, dict):
        return ""
    if "simpleText" in o:
        return o["simpleText"]
    if "runs" in o:
        return "".join(r.get("text", "") for r in o["runs"])
    if isinstance(o.get("content"), str):
        return o["content"]
    for k in ("snippet", "headline", "title", "sectionHeaderViewModel", "sectionHeader", "text"):
        if k in o:
            t = _text(o[k])
            if t:
                return t
    return ""


PROFILE = ROOT / "browser-profile"     # YouSNR's own browser profile (cookies, optional YouTube sign-in)


class _Session:
    """A browser window for one job: .ctx is the context, .close() shuts everything down."""
    def __init__(self, ctx, browser, name):
        self.ctx, self.browser, self.name = ctx, browser, name

    def close(self):
        for x in (self.ctx, self.browser):
            try:
                if x is not None:
                    x.close()
            except Exception:
                pass


def _launch(p, show, offscreen=False):
    """Open Chrome/Edge looking like a normal browser rather than an automated one.
    offscreen: a normal (non-headless) window parked off the screen.
    Uses YouSNR's saved profile only after `signin`; otherwise a fresh throwaway profile each time,
    so a stray Chrome left open can never block it."""
    args = ["--mute-audio", "--autoplay-policy=user-gesture-required", "--disable-blink-features=AutomationControlled"]
    if offscreen and not show:
        args += ["--window-position=-32000,-32000", "--window-size=1280,900"]
    headless = not (show or offscreen)
    common = dict(headless=headless, ignore_default_args=["--enable-automation"], args=args)
    view = dict(locale="en-US", viewport={"width": 1280, "height": 900})
    channels = ("chrome", "msedge", None)              # your installed Chrome, else Edge, else Playwright's

    if (PROFILE / ".signed-in").exists():
        for channel in channels:
            try:
                kw = dict(common, **view, **({"channel": channel} if channel else {}))
                ctx = p.chromium.launch_persistent_context(str(PROFILE), **kw)
                ctx.pages[0].evaluate("1") if ctx.pages else ctx.new_page().close()   # make sure it's alive
                return _Session(ctx, None, (channel or "chromium") + ", signed-in profile")
            except Exception:
                continue
        print("    · signed-in profile is busy or broken; using a fresh one")

    last = None
    for channel in channels:
        try:
            browser = p.chromium.launch(**common, **({"channel": channel} if channel else {}))
            return _Session(browser.new_context(**view), browser, channel or "chromium")
        except Exception as e:
            last = e
    if offscreen and not show:                       # no screen to put a window on: go fully hidden
        return _launch(p, False, False)
    raise RuntimeError(f"no browser found: install Chrome, or run  python -m playwright install chromium ({last})")


def signin():
    """Open YouSNR's saved browser profile so you can sign in to YouTube once; later runs reuse it."""
    from playwright.sync_api import sync_playwright
    PROFILE.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = None
        for channel in ("chrome", "msedge", None):
            try:
                ctx = p.chromium.launch_persistent_context(
                    str(PROFILE), headless=False, ignore_default_args=["--enable-automation"],
                    args=["--disable-blink-features=AutomationControlled"], **({"channel": channel} if channel else {}))
                break
            except Exception:
                continue
        if ctx is None:
            sys.exit("Couldn't open Chrome or Edge.")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/")
        print("Sign in to YouTube in the window that opened, then close that window.")
        try:
            while ctx.pages:
                ctx.pages[0].wait_for_timeout(1000)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
    (PROFILE / ".signed-in").write_text("1", encoding="utf-8")
    print("Saved. YouSNR will use this signed-in profile from now on (delete the browser-profile folder to undo).")


# Timestamps that are NOT transcript lines: video length on thumbnails, the player clock, chapter
# lists, timestamps in the description and comments. Ignore those everywhere.
_STAMPS_JS = r"""
const SKIP = '#movie_player, .html5-video-player, #related, ' +
  'ytd-watch-next-secondary-results-renderer, #comments, ytd-comments, ytd-thumbnail, yt-thumbnail-view-model, ' +
  'ytd-thumbnail-overlay-time-status-renderer, ytd-macro-markers-list-renderer, ytd-macro-markers-list-item-renderer, ' +
  '[target-id*="macro-markers"], [target-id*="chapters"], #description, ytd-text-inline-expander, ' +
  'ytd-structured-description-content-renderer, ytd-watch-metadata';
function transcriptStamps() {
  const re = /^(\d+:)?\d{1,2}:\d{2}$/, out = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walk.nextNode());) {
    const el = n.parentElement;
    if (el && re.test(n.nodeValue.trim()) && el.getClientRects().length && !el.closest(SKIP)) out.push(el);
  }
  const inPanel = out.filter(e => e.closest('ytd-engagement-panel-section-list-renderer'));
  return inPanel.length >= 3 ? inPanel : out;
}
"""

# Find the transcript on screen without relying on YouTube's element names: the smallest container
# holding most of the remaining timestamps is the transcript list. Read it.
_DOM_JS = "() => {" + _STAMPS_JS + r"""
  const stamps = transcriptStamps();
  if (stamps.length < 3) return {count: stamps.length, text: ''};
  const counts = new Map();
  for (const st of stamps) for (let e = st; e && e !== document.body; e = e.parentElement) counts.set(e, (counts.get(e) || 0) + 1);
  const need = Math.max(3, Math.floor(stamps.length * 0.7));
  let best = null, bestDepth = -1;
  for (const [e, c] of counts) {
    if (c < need) continue;
    let d = 0; for (let x = e; x; x = x.parentElement) d++;
    if (d > bestDepth) { best = e; bestDepth = d; }
  }
  return {count: stamps.length, text: best ? best.innerText : ''};
}"""

_COUNT_JS = "() => {" + _STAMPS_JS + " return transcriptStamps().length; }"

_DURATION_JS = r"""() => { try { const r = window.ytInitialPlayerResponse || document.getElementById('movie_player').getPlayerResponse();
  return +r.videoDetails.lengthSeconds || 0; } catch (e) { const v = document.querySelector('video'); return v && isFinite(v.duration) ? v.duration : 0; } }"""

_OPEN_JS = r"""() => {
  const p = document.querySelector('ytd-engagement-panel-section-list-renderer[target-id="engagement-panel-searchable-transcript"]');
  if (p) { p.setAttribute('visibility', 'ENGAGEMENT_PANEL_VISIBILITY_EXPANDED'); return true; }
  return false;
}"""


def fetch_browser(vid, show=False, offscreen=False):
    from playwright.sync_api import sync_playwright
    replies, log = [], []

    def say(msg):
        log.append(msg)
        print(f"    · {msg}")

    with sync_playwright() as p:
        browser = _launch(p, show, offscreen)
        ctx = browser.ctx
        say(f"opened {browser.name}" + ("" if show else " (off-screen window)" if offscreen else " (hidden)"))
        try:
            if BASE.startswith("https://www.youtube.com"):
                ctx.add_cookies([{"name": "SOCS", "value": "CAI", "domain": ".youtube.com", "path": "/"}])
            page = ctx.new_page()
        except Exception:
            browser.close()
            raise RuntimeError("the browser closed as soon as it opened") from None
        closed = []
        page.on("close", lambda *_: closed.append(True))
        page.on("crash", lambda *_: say("the YouTube tab crashed"))

        def on_response(r):
            if "/youtubei/" in r.url and r.request.method == "POST":
                try:
                    replies.append((r.url.split("?")[0].rsplit("/", 1)[-1], r.json()))
                except Exception:
                    pass
        page.on("response", on_response)

        def snapshot(tag):
            if DEBUG_DIR and not closed:
                try:
                    DEBUG_DIR.mkdir(exist_ok=True)
                    page.screenshot(path=str(DEBUG_DIR / f"{vid}.browser{tag}.png"))
                    _dump(f"{vid}.browser{tag}.html", page.content())
                except Exception:
                    pass

        def count():
            try:
                return page.evaluate(_COUNT_JS)
            except Exception:
                return -1

        best, dom, base_stamps, duration = ([], [], ""), None, 0, 0
        try:
            page.goto(f"{BASE}/watch?v={vid}&hl=en", wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("#description, ytd-watch-metadata", timeout=20000)
                say("video page loaded")
            except Exception:
                say("video page loaded, but no description box found")
            page.evaluate("() => { const v = document.querySelector('video'); if (v) v.pause(); }")
            base_stamps = max(count(), 0)
            try:
                duration = page.evaluate(_DURATION_JS) or 0
            except Exception:
                duration = 0

            def opened():
                if any(_parse_panel(body)[0] for _, body in replies):
                    return True
                return count() >= base_stamps + 3

            steps = [
                ('"...more" (description)', lambda: page.locator(
                    "tp-yt-paper-button#expand, #description-inline-expander #expand, #expand").first.click(timeout=4000)),
                ('"Show transcript"', lambda: page.get_by_role(
                    "button", name=re.compile(r"show transcript", re.I)).first.click(timeout=4000)),
                ('"Transcript" tab', lambda: page.get_by_role(
                    "tab", name=re.compile(r"^transcript$", re.I)).first.click(timeout=3000)),
                ('"Transcript" button', lambda: page.get_by_role(
                    "button", name=re.compile(r"^transcript$", re.I)).first.click(timeout=3000)),
                ("transcript panel (direct)", lambda: page.evaluate(_OPEN_JS)),
            ]
            for name, step in steps:
                if closed or opened():
                    break
                try:
                    step()
                    say(f"clicked {name}")
                except Exception:
                    say(f"no {name} to click")
                if name.startswith('"...more"'):             # the description itself has timestamps:
                    page.wait_for_timeout(800)                # don't mistake those for the transcript
                    base_stamps = max(count(), base_stamps)
                    continue
                for _ in range(12):
                    if closed or opened():
                        break
                    page.wait_for_timeout(500)

            for _ in range(10):
                if closed or opened():
                    break
                page.wait_for_timeout(500)
            if not closed:
                page.wait_for_timeout(800)
                say(f"timestamps on screen: {count()} (before: {base_stamps}); YouTube replies: "
                    f"{', '.join(sorted({n for n, _ in replies})) or 'none'}")
            snapshot("")
            if DEBUG_DIR:
                for i, (name, body) in enumerate(replies):
                    _dump(f"{vid}.reply{i:02d}.{name}.json", body)

            best = max((_parse_panel(b) for _, b in replies), key=lambda r: len(r[0]), default=([], [], ""))
            if not best[0] and not closed:
                dom = page.evaluate(_DOM_JS)
        except Exception as e:
            snapshot(".error")
            if closed or "closed" in str(e).lower():
                raise RuntimeError("the browser window closed before the transcript was read "
                                   "(don't close it while it works; it closes itself)") from None
            raise
        finally:
            try:
                browser.close()
            except Exception:
                pass

    segs, chapters, label = best
    how = "browser (YouTube panel reply)"
    if not segs and dom:
        how = "browser (read from the panel on screen)"
        segs, chapters = parse_pasted(dom.get("text", ""))
        last = segs[-1]["start"] if segs else 0
        if segs and (len(chapters) > max(3, len(segs) // 2) or (duration > 120 and last < duration * 0.5)):
            raise RuntimeError(f"what's on screen doesn't look like the transcript ({len(segs)} lines up to "
                               f"{ts(last)} of {ts(duration)}, {len(chapters)} headings)")
        for s in segs:
            s["label"] = ts(s["start"])
    if not segs:
        raise RuntimeError("couldn't open the transcript panel (try --show to watch, --debug to save what happened)")
    track = {"language": label, "generated": "auto-generated" in label.lower() if label else None,
             "source": "youtube-panel"}
    return segs, chapters, track, how


def _parse_get_panel(resp):
    """YouTube's 2026 transcript panel ("In this video" → Transcript), delivered by youtubei/v1/get_panel:
    sections of macroMarkersPanelItemViewModel → timelineItemViewModel → transcriptSegmentViewModel,
    each section headed by a timelineChapterViewModel."""
    segs, chapters = [], []
    sections = [v for k, v in _walk(resp) if k == "itemSectionRenderer" and isinstance(v, dict)]
    for sec in sections:
        hdr = None
        for k, v in _walk(sec.get("header") or {}):
            if k == "timelineChapterViewModel" and isinstance(v, dict):
                hdr = clean(_text(v.get("title")))
                break
        first = True
        for it in sec.get("contents") or []:
            m = (it or {}).get("macroMarkersPanelItemViewModel") or {}
            tl = (m.get("item") or {}).get("timelineItemViewModel") or {}
            we = next((v for k, v in _walk(m.get("onTap") or {}) if k == "watchEndpoint" and isinstance(v, dict)), {})
            for ci in tl.get("contentItems") or []:
                t = (ci or {}).get("transcriptSegmentViewModel")
                if not t:
                    continue
                text = clean(_text(t.get("simpleText")) or _text(t.get("text")) or _text(t))
                label = t.get("timestamp") or tl.get("timestamp") or ""
                if not text:
                    continue
                if "startTimeSeconds" in we:
                    start = float(we["startTimeSeconds"])
                elif re.fullmatch(r"(\d+:)?\d{1,2}:\d{2}", label):
                    start = float(parse_ts(label))
                else:
                    continue
                if hdr and first:
                    chapters.append({"start": int(start), "title": hdr})
                    first = False
                segs.append({"start": start, "dur": None, "text": text, "label": label or ts(start)})
    segs.sort(key=lambda x: x["start"])
    for a, b in zip(segs, segs[1:]):
        a["dur"] = round(b["start"] - a["start"], 3)
    return segs, chapters


def _parse_panel(resp):
    segs, chapters, label = [], [], ""
    if any(k == "transcriptSegmentViewModel" for k, _ in _walk(resp)):
        segs, chapters = _parse_get_panel(resp)
        if segs:
            return segs, chapters, label
    lists = [v for k, v in _walk(resp) if k == "initialSegments" and isinstance(v, list)]
    items = [next(iter(i.items())) for i in (lists[0] if lists else []) if isinstance(i, dict) and i]
    if not items:   # unknown newer layout: any object carrying startMs + text
        items = [(k, v) for k, v in _walk(resp) if isinstance(v, dict) and "startMs" in v
                 and any(x in v for x in ("snippet", "text", "title", "headline"))]
    for kind, r in items:
        if not isinstance(r, dict):
            continue
        if "Header" in kind:                                    # chapter heading in the panel
            t = clean(_text(r.get("snippet")) or _text(r))
            if t and "startMs" in r:
                chapters.append({"start": int(r["startMs"]) // 1000, "title": t})
            continue
        if "startMs" not in r:
            continue
        text = clean(_text(r.get("snippet")) or _text(r.get("text")))
        if not text:
            continue
        start, end = int(r["startMs"]) / 1000, int(r.get("endMs") or r["startMs"]) / 1000
        segs.append({"start": round(start, 3), "dur": round(end - start, 3), "text": text,
                     "label": _text(r.get("startTimeText")) or None})
    for k, v in _walk(resp):                                   # selected language, e.g. "English (auto-generated)"
        if k == "subMenuItems" and isinstance(v, list):
            sel = [i for i in v if i.get("selected")]
            if sel:
                label = _text(sel[0].get("title"))
                break
    return segs, chapters, label


# ---- captions through the real video player --------------------------------
# When YouTube rate-limits plain caption downloads from your IP, it usually still serves them to
# a real browser playing the video (those requests carry the player's own proof-of-origin token).
# So: open the video in a hidden Chrome/Edge, switch CC on, and keep the caption file the player loads.

class TranscriptsDisabled(Exception):
    pass


def parse_caption_body(body):
    """Caption file (json3, srv3 or srv1) -> [(start_s, dur_s, text)], one entry per cue."""
    body = body.strip()
    cues = []
    if body.startswith("{"):
        for e in json.loads(body).get("events", []):
            if "segs" not in e:
                continue
            t = clean("".join(x.get("utf8", "") for x in e["segs"]))
            if t:
                cues.append((e["tStartMs"] / 1000, e.get("dDurationMs", 0) / 1000, t))
    elif 'format="3"' in body[:300]:
        for m in re.finditer(r"<p\b([^>]*)>(.*?)</p>", body, re.S):
            a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            t = clean(re.sub(r"<[^>]+>", "", m.group(2)))
            if t and "t" in a:
                cues.append((int(a["t"]) / 1000, int(a.get("d", 0)) / 1000, t))
    else:
        for m in re.finditer(r"<text\b([^>]*)>(.*?)</text>", body, re.S):
            a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            t = clean(html.unescape(re.sub(r"<[^>]+>", "", m.group(2))))
            if t and "start" in a:
                cues.append((float(a["start"]), float(a.get("dur", 0)), t))
    return cues


_CC_API_JS = r"""() => {
  const p = document.getElementById('movie_player'); if (!p) return 'no player';
  try { p.mute && p.mute(); } catch (e) {}
  try { p.loadModule && p.loadModule('captions'); } catch (e) {}
  try { p.toggleSubtitlesOn && p.toggleSubtitlesOn(); } catch (e) {}
  try { p.setOption && p.setOption('captions', 'reload', true); } catch (e) {}
  return 'ok';
}"""

_PLAY_JS = r"""() => { const p = document.getElementById('movie_player');
  try { p.mute(); p.playVideo(); } catch (e) { const v = document.querySelector('video'); if (v) { v.muted = true; v.play(); } } }"""

_TRACKS_JS = r"""() => { try {
  const r = window.ytInitialPlayerResponse || (document.getElementById('movie_player').getPlayerResponse());
  if (!r) return null;
  const t = ((r.captions || {}).playerCaptionsTracklistRenderer || {}).captionTracks;
  return (t || []).map(x => ({code: x.languageCode, kind: x.kind || '',
                              name: x.name ? (x.name.simpleText || (x.name.runs || []).map(r => r.text).join('')) : ''}));
} catch (e) { return null } }"""


def fetch_player_captions(vid, show=False, save_raw=False):
    from playwright.sync_api import sync_playwright
    got = []

    def say(msg):
        print(f"    · {msg}")

    with sync_playwright() as p:
        browser = _launch(p, show, offscreen=not show)
        ctx = browser.ctx
        try:
            if BASE.startswith("https://www.youtube.com"):
                ctx.add_cookies([{"name": "SOCS", "value": "CAI", "domain": ".youtube.com", "path": "/"}])
            page = ctx.new_page()

            def on_response(r):
                if "/api/timedtext" in r.url and f"v={vid}" in r.url and r.status == 200:
                    try:
                        body = r.text()
                        if body.strip():
                            got.append((r.url, body))
                    except Exception:
                        pass
            page.on("response", on_response)

            page.goto(f"{BASE}/watch?v={vid}&hl=en", wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("#movie_player, .html5-video-player", timeout=20000)
            except Exception:
                say("no video player on the page")
            tracks = page.evaluate(_TRACKS_JS)
            if tracks == []:
                raise TranscriptsDisabled("this video has no captions")

            steps = [("CC button", lambda: page.locator(".ytp-subtitles-button").first.click(timeout=5000, force=True)),
                     ("player captions API", lambda: page.evaluate(_CC_API_JS)),
                     ("play muted", lambda: page.evaluate(_PLAY_JS)),
                     ("CC button again", lambda: page.locator(".ytp-subtitles-button").first.click(timeout=5000, force=True))]
            for name, act in steps:
                if got:
                    break
                try:
                    act()
                    say(f"tried {name}")
                except Exception:
                    say(f"couldn't use {name}")
                for _ in range(20):
                    if got:
                        break
                    page.wait_for_timeout(500)
            if not got:                                   # maybe an ad is playing first
                say("waiting for the video to start (ads?)")
                for _ in range(60):
                    if got:
                        break
                    try:
                        skip = page.locator(".ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern")
                        if skip.count() and skip.first.is_visible():
                            skip.first.click(timeout=2000)
                            say("skipped an ad")
                        cc = page.locator(".ytp-subtitles-button[aria-pressed='false']")
                        if cc.count() and not page.locator(".ad-showing").count():
                            cc.first.click(timeout=2000, force=True)
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if not got:
        raise RuntimeError("the video player didn't load any captions")
    if save_raw:
        out = ROOT / "debug" / "raw"
        out.mkdir(parents=True, exist_ok=True)
        for i, (u, b) in enumerate(got):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
            tag = f"{vid}.player{i}.{(q.get('lang') or ['x'])[0]}{'.auto' if (q.get('kind') or [''])[0] == 'asr' else ''}"
            fmt = (q.get("fmt") or ["default"])[0]
            (out / f"{tag}.{fmt}.txt").write_text(b, encoding="utf-8")
            (out / f"{tag}.url.txt").write_text(re.sub(r"(pot|signature|sparams|ei|key)=[^&]+", r"\1=…", u), encoding="utf-8")
            say(f"saved debug/raw/{tag}.{fmt}.txt ({len(b):,} chars)")
    url, body = max(got, key=lambda g: len(g[1]))
    cues = parse_caption_body(body)
    if not cues:
        raise RuntimeError("the player's caption file was empty")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    code, kind = (q.get("lang") or [""])[0], (q.get("kind") or [""])[0]
    name = next((t["name"] for t in (tracks or []) if t["code"] == code and t["kind"] == kind), "") or code
    say(f"got {len(cues)} caption cues ({name})")
    return group_cues(cues), {"language": name, "language_code": code, "generated": kind == "asr",
                              "source": "player-captions-grouped"}


def fetch_captions_fallback(vid, langs):
    """Plan B: the caption file (finer-grained than the panel), regrouped to roughly match it."""
    from youtube_transcript_api import YouTubeTranscriptApi
    tl = YouTubeTranscriptApi().list(vid)
    try:
        tr = tl.find_manually_created_transcript(langs)
    except Exception:
        try:
            tr = tl.find_generated_transcript(langs)
        except Exception:
            tr = next(iter(tl))
    raw = [(s.start, s.duration, clean(s.text)) for s in tr.fetch().snippets if clean(s.text)]
    return group_cues(raw), {"language": tr.language, "language_code": tr.language_code,
                             "generated": tr.is_generated, "source": "captions-grouped"}


PANEL_MIN_CHARS = 100


def group_cues(raw, min_chars=PANEL_MIN_CHARS):
    """Rebuild the panel's lines from caption cues: YouTube glues whole cues together until a line
    reaches ~100 characters (matches every panel line checked so far, auto and creator captions)."""
    segs, cur = [], None
    for start, dur, text in raw:
        if cur and len(cur["text"]) < min_chars:
            cur["text"] += " " + text
            cur["dur"] = round(start + dur - cur["start"], 3)
        else:
            cur = {"start": round(start, 3), "dur": round(dur, 3), "text": text}
            segs.append(cur)
    for s in segs:
        s["label"] = ts(s["start"])
    return segs


def dump_raw(vid, lang="en"):
    """Save the caption track in every format YouTube offers, to work out how the panel groups lines."""
    from youtube_transcript_api import YouTubeTranscriptApi
    tl = YouTubeTranscriptApi().list(vid)
    out = ROOT / "debug" / "raw"
    out.mkdir(parents=True, exist_ok=True)
    tracks = list(tl)
    print("  tracks: " + ", ".join(f"{t.language_code}{'(auto)' if t.is_generated else ''}" for t in tracks))
    picks = [t for t in tracks if t.language_code.startswith(lang)] or tracks[:1]
    for tr in picks:
        tag = f"{vid}.{tr.language_code}{'.auto' if tr.is_generated else ''}"
        base = tr._url
        for fmt in ("default", "srv1", "srv2", "srv3", "json3", "vtt"):
            url = base if fmt == "default" else f"{base}&fmt={fmt}"
            try:
                r = tr._http_client.get(url)
                (out / f"{tag}.{fmt}.txt").write_text(r.text, encoding="utf-8")
                print(f"  saved debug/raw/{tag}.{fmt}.txt ({len(r.text):,} chars, HTTP {r.status_code})")
            except Exception as e:
                print(f"  {fmt}: {e.__class__.__name__}")


def read_clipboard():
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        text = root.clipboard_get()
        root.destroy()
        return text
    except Exception:
        pass
    import subprocess
    for cmd in (["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], ["pbpaste"],
                ["xclip", "-selection", "clipboard", "-o"]):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=10).stdout
        except Exception:
            continue
    sys.exit("Couldn't read the clipboard. Save the transcript to a .txt file and pass its name instead.")


def parse_pasted(text):
    """Turn text copied out of YouTube's transcript panel back into segments + chapters.

    The panel copies as: [chapter title] / timestamp / caption line / timestamp / caption line ...
    A line that isn't a timestamp and isn't directly after one is a chapter heading.
    """
    spoken = re.compile(r"^\d+ (hours?|minutes?|seconds?)(,? \d+ (hours?|minutes?|seconds?))*$", re.I)
    lines = [l.strip() for l in text.splitlines() if l.strip() and not spoken.match(l.strip())]
    segs, chapters, pending_titles, i = [], [], [], 0
    stamp = re.compile(r"^(?:\d+:)?\d{1,2}:\d{2}$")
    while i < len(lines):
        if stamp.match(lines[i]):
            start = parse_ts(lines[i])
            for t in pending_titles:
                chapters.append({"start": start, "title": t})
            pending_titles = []
            body = lines[i + 1] if i + 1 < len(lines) and not stamp.match(lines[i + 1]) else ""
            segs.append({"start": start, "dur": None, "text": clean(body)})
            i += 2 if body else 1
        else:
            if lines[i].lower() not in UI_LABELS:
                pending_titles.append(lines[i])
            i += 1
    for a, b in zip(segs, segs[1:]):
        a["dur"] = b["start"] - a["start"]
    return segs, chapters


# ---------------------------------------------------------------- rendering

def render_page(d):
    e = html.escape
    vid = d["id"]
    chapters = sorted(d.get("chapters") or [], key=lambda c: c["start"])
    fixed = (d.get("corrected") or {}).get("lines") or {}
    nfix = sum(1 for pieces in fixed.values() for _, o in pieces if o is not None)

    def text_html(i, s):
        pieces = fixed.get(str(i))
        if not pieces:
            return e(s["text"])
        out = []
        for shown, orig in pieces:
            if orig is None:
                out.append(e(shown))
            else:
                tip = f'YouTube: {orig}' if orig else "YouTube's transcript leaves this out"
                out.append(f'<span class="fix" data-o="{e(orig)}" title="{e(tip)}">{e(shown)}</span>')
        return " ".join(out)

    rows, ci = [], 0
    for i, s in enumerate(d["segments"]):
        while ci < len(chapters) and chapters[ci]["start"] <= s["start"]:
            rows.append(f'<h2 class="ch" id="ch{ci}">{e(chapters[ci]["title"])}</h2>')
            ci += 1
        t = int(s["start"])
        rows.append(
            f'<a class="seg" href="https://www.youtube.com/watch?v={vid}&amp;t={t}s" target="_blank" rel="noopener" '
            f'data-s="{s["start"]}" data-d="{s.get("dur") or ""}">'
            f'<span class="ts">{e(s.get("label") or ts(s["start"]))}</span><span class="tx">{text_html(i, s)}</span></a>')
    toc = "".join(
        f'<a class="chap" href="#ch{i}" data-ch="{i}"><span class="ct">{e(c["title"])}</span>'
        f'<span class="ts">{ts(c["start"])}</span></a>' for i, c in enumerate(chapters))

    title = d.get("title") or vid
    plain = " ".join(s["text"] for s in d["segments"])
    desc = (plain[:155] + "…") if len(plain) > 155 else plain
    pub = d.get("published") or ""
    if re.fullmatch(r"\d{8}", pub):
        dt = datetime.date(int(pub[:4]), int(pub[4:6]), int(pub[6:]))
        pub = f"{dt:%b} {dt.day}, {dt.year}"
    lang = d.get("track", {})
    track = lang.get("language") or ""
    if lang.get("generated") and "auto-generated" not in track.lower():
        track += " (auto-generated)"

    tpl = (TEMPLATES / "page.html").read_text(encoding="utf-8")
    return (tpl.replace("{{TITLE}}", e(title))
               .replace("{{DESC}}", e(desc))
               .replace("{{ID}}", vid)
               .replace("{{CANON}}", f"{SITE}/{vid}/")
               .replace("{{CHANNEL}}", e(d.get("channel") or ""))
               .replace("{{PUBLISHED}}", e(pub))
               .replace("{{TRACK}}", e(track))
               .replace("{{TOC}}", toc)
               .replace("{{TOC_CLASS}}", "" if chapters else "hidden")
               .replace("{{FIXNOTE}}", (f'<span class="fixnote"> · {nfix} word{"s" if nfix != 1 else ""} corrected</span>'
                                         if nfix else ""))
               .replace("{{FIXCLASS}}", "" if nfix else "hidden")
               .replace("{{ROWS}}", "\n".join(rows)))


def run_correction(vid, mode="selective", engines=None, log=print):
    """Correct one saved video and republish its page. Returns the summary, or None if not possible."""
    if acc is None or not acc.available():
        return None
    f = DATA / f"{vid}.json"
    if not f.exists():
        raise RuntimeError(f"{vid} isn't on the site yet")
    d = json.loads(f.read_text(encoding="utf-8"))
    r = acc.correct(d, mode=mode, engines=engines, meta=d, log=log)
    save(d)
    write_page(d)
    log(f"  corrections ({mode}): {r['fixes']} word change(s); checked {r['checked_seconds']:.0f}s of audio "
        f"with {', '.join(r['engines']) or 'no engine'}; cost ${r['usd']:.3f} "
        f"(this month ${acc.spent_this_month():.2f} of ${acc.settings()['budget']:.2f})")
    return r


INDEX = DATA / "_index.json"


def _load_index():
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_page(d):
    out = DOCS / d["id"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render_page(d), encoding="utf-8")


def write_indexes(index=None):
    """Home page, ids.json, sitemap and the fixed files. Cheap: reads only data/_index.json."""
    index = _load_index() if index is None else index
    items = sorted(({"id": k, **v} for k, v in index.items()), key=lambda d: d.get("added") or "", reverse=True)
    DOCS.mkdir(exist_ok=True)
    home = (TEMPLATES / "home.html").read_text(encoding="utf-8")       # no public list of past videos
    (DOCS / "index.html").write_text(home, encoding="utf-8")
    (DOCS / "404.html").write_text((TEMPLATES / "404.html").read_text(encoding="utf-8"), encoding="utf-8")
    (DOCS / "CNAME").write_text("yousnr.com\n", encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    (DOCS / "ids.json").write_text(json.dumps([d["id"] for d in items]), encoding="utf-8")
    urls = [f"{SITE}/"] + [f"{SITE}/{d['id']}/" for d in items]
    (DOCS / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{u}</loc></url>\n" for u in urls) + "</urlset>\n", encoding="utf-8")
    (DOCS / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n", encoding="utf-8")
    return len(items)


def build():
    """Rebuild every page and the index from data/."""
    index = {}
    for f in sorted(DATA.glob("*.json")):
        if f.name.startswith("_"):
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        write_page(d)
        index[d["id"]] = {"title": d.get("title", ""), "channel": d.get("channel", ""), "added": d.get("added", "")}
    DATA.mkdir(exist_ok=True)
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=0), encoding="utf-8")
    n = write_indexes(index)
    print(f"Built {n} page(s) into docs/")


_save_lock = threading.Lock()


def save(d):
    DATA.mkdir(exist_ok=True)
    f = DATA / f"{d['id']}.json"
    if f.exists():
        d.setdefault("added", json.loads(f.read_text(encoding="utf-8")).get("added"))
    if not d.get("added"):
        d["added"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    f.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    with _save_lock:
        index = _load_index()
        index[d["id"]] = {"title": d.get("title", ""), "channel": d.get("channel", ""), "added": d["added"]}
        INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"  saved data/{d['id']}.json  ({len(d['segments'])} lines, {len(d.get('chapters', []))} chapters)")


# ---------------------------------------------------------------- server
# `serve` runs on your own computer. yousnr.com (GitHub Pages) asks it for videos it doesn't
# have yet, through a Cloudflare Tunnel at api.yousnr.com. Each video is fetched from YouTube
# once; after that it's a static page for everyone.

ALLOWED_ORIGINS = {"https://yousnr.com", "https://www.yousnr.com", "https://yoursnr.com", "https://www.yoursnr.com"}
PER_VISITOR_PER_HOUR = 20     # new (uncached) videos one visitor can trigger per hour
MIN_GAP_SECONDS = 3           # pause between YouTube fetches
BLOCK_COOLDOWN = 15 * 60      # back off this long if YouTube says "too many requests"
MISSING_RETRY = 24 * 3600     # re-check videos without captions after a day
PUSH_EVERY = 5 * 60           # publish new pages to GitHub at most this often
MISSING = DATA / "_missing.json"

NO_TRANSCRIPT = {"TranscriptsDisabled", "NoTranscriptFound", "NoTranscriptAvailable"}
UNAVAILABLE = {"VideoUnavailable", "VideoUnplayable", "AgeRestricted", "InvalidVideoId", "NotTranslatable"}
BLOCKED = {"RequestBlocked", "IpBlocked", "TooManyRequests", "PoTokenRequired"}


class Corrector:
    """Background worker: selective check for every new video, full re-transcription once it's popular."""
    VIEWS = DATA / "_views.json"

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.jobs, self.queued = [], set()
        self.cv = threading.Condition()
        try:
            self.views = json.loads(self.VIEWS.read_text(encoding="utf-8"))
        except Exception:
            self.views = {}
        self.enabled = acc is not None and bool(acc.available())
        if self.enabled:
            threading.Thread(target=self.loop, daemon=True).start()

    def enqueue(self, vid, mode):
        if not self.enabled or (vid, mode) in self.queued:
            return
        with self.cv:
            self.queued.add((vid, mode))
            self.jobs.append((vid, mode))
            self.cv.notify()

    def viewed(self, vid):
        f = DATA / f"{vid}.json"
        if not f.exists():
            return
        n = self.views[vid] = self.views.get(vid, 0) + 1
        self.VIEWS.write_text(json.dumps(self.views), encoding="utf-8")
        if self.enabled and n >= acc.settings()["full_at"]:
            try:
                mode = (json.loads(f.read_text(encoding="utf-8")).get("corrected") or {}).get("mode")
            except Exception:
                mode = None
            if mode != "full":
                self.enqueue(vid, "full")

    def loop(self):
        while True:
            with self.cv:
                while not self.jobs:
                    self.cv.wait()
                vid, mode = self.jobs.pop(0)
            try:
                print(f"{time.strftime('%H:%M:%S')}  {vid}  checking words ({mode})")
                if run_correction(vid, mode, log=lambda m: print(m)):
                    self.fetcher.pending.append(vid)
            except Exception as e:
                print(f"  correction skipped: {e.__class__.__name__}: {str(e)[:200]}")
            finally:
                self.queued.discard((vid, mode))


class Fetcher:
    def __init__(self, lang="en", browser=True):
        self.lang = lang
        self.browser = browser
        self.lock = threading.Lock()
        self.last_fetch = 0.0
        self.cooldown_until = 0.0
        self.visitors = {}
        self.pending = []          # IDs fetched since the last push
        try:
            self.missing = json.loads(MISSING.read_text(encoding="utf-8"))
        except Exception:
            self.missing = {}

    def _visitor_ok(self, ip):
        now = time.time()
        hits = [t for t in self.visitors.get(ip, []) if now - t < 3600]
        self.visitors[ip] = hits
        if len(hits) >= PER_VISITOR_PER_HOUR:
            return False
        hits.append(now)
        return True

    def page(self, vid, ip):
        """Returns (status, content_type, body)."""
        f = DATA / f"{vid}.json"
        if f.exists():
            return 200, "text/html; charset=utf-8", render_page(json.loads(f.read_text(encoding="utf-8")))
        miss = self.missing.get(vid)
        if miss and time.time() - miss["at"] < (30 * 86400 if miss["reason"] == "music" else MISSING_RETRY):
            return 404, "application/json", json.dumps({"error": miss["reason"]})
        if time.time() < self.cooldown_until:
            return 503, "application/json", json.dumps({"error": "busy", "retry": int(self.cooldown_until - time.time())})
        if not self._visitor_ok(ip):
            return 429, "application/json", json.dumps({"error": "slow_down"})
        if not self.lock.acquire(timeout=120):
            return 503, "application/json", json.dumps({"error": "busy", "retry": 60})
        try:
            if f.exists():                                  # someone else fetched it while we waited
                return 200, "text/html; charset=utf-8", render_page(json.loads(f.read_text(encoding="utf-8")))
            if time.time() < self.cooldown_until:
                return 503, "application/json", json.dumps({"error": "busy", "retry": int(self.cooldown_until - time.time())})
            wait = MIN_GAP_SECONDS - (time.time() - self.last_fetch)
            if wait > 0:
                time.sleep(wait)
            self.last_fetch = time.time()
            try:
                d = get_transcript(vid, self.lang, browser=self.browser)
            except Exception as e:
                name = e.__class__.__name__
                msg = str(e)
                if name in BLOCKED or "429" in msg or "Too Many Requests" in msg:
                    self.cooldown_until = time.time() + BLOCK_COOLDOWN
                    print(f"  YouTube says slow down ({name}); pausing new fetches for {BLOCK_COOLDOWN // 60} min")
                    return 503, "application/json", json.dumps({"error": "busy", "retry": BLOCK_COOLDOWN})
                reason = ("music" if name == "MusicVideo" else "no_captions" if name in NO_TRANSCRIPT
                          else "unavailable" if name in UNAVAILABLE else None)
                if reason:
                    self.missing[vid] = {"reason": reason, "at": time.time()}
                    MISSING.write_text(json.dumps(self.missing), encoding="utf-8")
                    return 404, "application/json", json.dumps({"error": reason})
                print(f"  failed: {name}: {msg.splitlines()[0] if msg else ''}")
                return 502, "application/json", json.dumps({"error": "failed"})
            self.pending.append(vid)
            if getattr(self, "corrector", None):
                self.corrector.enqueue(vid, "selective")
            return 200, "text/html; charset=utf-8", render_page(d)
        finally:
            self.lock.release()


def publisher(fetcher, every):
    """Commit and push new pages to GitHub in batches, so they become static and instant."""
    if not (ROOT / ".git").exists():
        print("  (not a git repo: new pages won't be published to GitHub)")
        return
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    last = 0.0
    while True:
        time.sleep(15)
        if not fetcher.pending or time.time() - last < every:
            continue
        ids, fetcher.pending = fetcher.pending[:], []
        last = time.time()

        def git(*a):
            return subprocess.run(["git", *a], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
        git("add", "-A", "data", "docs")
        c = git("commit", "-m", f"add {len(ids)} transcript(s): {' '.join(ids[:10])}")
        if c.returncode != 0 and "nothing to commit" in (c.stdout + c.stderr):
            continue
        r = git("push")
        if r.returncode == 0:
            print(f"  published {len(ids)} new page(s) to GitHub")
        else:
            print(f"  git push failed (will retry): {(r.stderr or r.stdout).strip().splitlines()[-1:]}")
            fetcher.pending = ids + fetcher.pending
            last = time.time() - every + 60       # retry in about a minute


TUNNEL_TOKEN_FILE = ROOT / "tunnel-token.txt"   # never commit this (it's in .gitignore)


def start_tunnel():
    """Run the Cloudflare Tunnel alongside the server, so closing the window turns both off."""
    import shutil
    if not TUNNEL_TOKEN_FILE.exists():
        print("  tunnel: off (no tunnel-token.txt), so only this computer can reach the server")
        return None
    token = TUNNEL_TOKEN_FILE.read_text(encoding="utf-8").strip().split()[-1]   # tolerate a pasted full command
    exe = (os.environ.get("YOUSNR_CLOUDFLARED") or shutil.which("cloudflared")
           or next((c for c in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
                                r"C:\Program Files\cloudflared\cloudflared.exe") if Path(c).exists()), None))
    if not exe:
        print("  tunnel: cloudflared isn't installed. Run:  winget install --id Cloudflare.cloudflared")
        return None
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "run", "--token", token],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")

    def watch():
        connected = False
        log = (ROOT / "tunnel.log").open("w", encoding="utf-8")
        for line in proc.stderr:
            log.write(line)
            log.flush()
            if "Registered tunnel connection" in line and not connected:
                connected = True
                print("  tunnel: connected, api.yousnr.com is live")
            elif " ERR " in line and "Unauthorized" in line:
                print("  tunnel: the token in tunnel-token.txt was rejected; copy it again from Cloudflare")
        print("  tunnel: stopped (see tunnel.log)")
    threading.Thread(target=watch, daemon=True).start()
    print("  tunnel: connecting…")
    return proc


def stop_tunnel(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def serve(port=8787, lang="en", push=True, push_every=PUSH_EVERY, host="127.0.0.1", tunnel=True, browser=True):
    import http.server
    fetcher = Fetcher(lang, browser)
    local = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    write_indexes()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(DOCS), **k)

        def log_message(self, *a):
            pass

        def _cors(self):
            origin = self.headers.get("Origin", "")
            if origin in ALLOWED_ORIGINS or origin in local:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def _send(self, status, ctype, body):
            b = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(b)
            except (BrokenPipeError, ConnectionResetError):
                pass                       # visitor closed the tab before it finished

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET")
            self.end_headers()

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/health":
                return self._send(200, "application/json", json.dumps(
                    {"ok": True, "videos": len(_load_index()), "paused_for": max(0, int(fetcher.cooldown_until - time.time()))}))
            m = re.fullmatch(r"/api/page/([A-Za-z0-9_-]{11})", path)
            if m:
                vid = m.group(1)
                ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
                t0 = time.time()
                status, ctype, body = fetcher.page(vid, ip)
                print(f"{time.strftime('%H:%M:%S')}  {vid}  {status}  {time.time() - t0:.1f}s  {ip}")
                return self._send(status, ctype, body)
            if path.startswith("/api/"):
                return self._send(404, "application/json", json.dumps({"error": "not_found"}))
            return super().do_GET()        # local preview of the site, like GitHub Pages

        def do_POST(self):
            m = re.fullmatch(r"/api/view/([A-Za-z0-9_-]{11})", urllib.parse.urlparse(self.path).path)
            if m:
                corrector.viewed(m.group(1))
                return self._send(204, "text/plain", "")
            return self._send(404, "application/json", json.dumps({"error": "not_found"}))

        def send_error(self, code, message=None, explain=None):
            if code == 404:
                return self._send(404, "text/html; charset=utf-8", (DOCS / "404.html").read_text(encoding="utf-8"))
            return super().send_error(code, message, explain)

    corrector = fetcher.corrector = Corrector(fetcher)
    if push:
        threading.Thread(target=publisher, args=(fetcher, push_every), daemon=True).start()
    srv = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f"YouSNR is ON   (close this window to turn it off)")
    print(f"  local:  http://localhost:{port}/watch?v=265-EZJ_aJE")
    if corrector.enabled:
        st = acc.settings()
        print(f"  corrections: on ({', '.join(acc.available())}); budget ${acc.spent_this_month():.2f} of "
              f"${st['budget']:.2f} used this month; full re-transcription at {st['full_at']} views")
    else:
        print("  corrections: off (set ELEVENLABS_API_KEY / ASSEMBLYAI_API_KEY to turn on)")
    proc = start_tunnel() if tunnel else None
    print()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        stop_tunnel(proc)


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("videos", nargs="+"); a.add_argument("--lang", default="en")
    a.add_argument("--debug", action="store_true", help="save YouTube's replies and a screenshot to debug/")
    a.add_argument("--browser", action="store_true", help=argparse.SUPPRESS)   # now the default
    a.add_argument("--captions", action="store_true", help="skip the transcript panel; use the caption file only (faster)")
    a.add_argument("--player", action="store_true", help="get captions through the video player, like a viewer turning on CC")
    a.add_argument("--allow-music", action="store_true", help="add it even if YouTube marks it as music")
    a.add_argument("--no-correct", action="store_true", help="skip the speech-recognition check")
    a.add_argument("--show", action="store_true", help="show the browser window while it works")
    p = sub.add_parser("paste"); p.add_argument("video"); p.add_argument("file", nargs="?", help="omit to read the clipboard")
    p.add_argument("--title", default=""); p.add_argument("--channel", default="")
    p.add_argument("--no-meta", action="store_true", help="don't look up title/channel online")
    sub.add_parser("build")
    sub.add_parser("signin", help="sign YouSNR's browser in to YouTube (optional, helps the panel load)")
    co = sub.add_parser("correct", help="fix likely mistakes in YouTube's words (selective, or --full)")
    co.add_argument("videos", nargs="+"); co.add_argument("--full", action="store_true")
    co.add_argument("--engines", default="", help="e.g. elevenlabs,assemblyai (default: every engine with a key)")
    sub.add_parser("usage", help="speech-recognition spending this month")
    bo = sub.add_parser("bakeoff", help="compare the engines with YouTube on a few minutes of a video")
    bo.add_argument("video"); bo.add_argument("--minutes", type=float, default=5)
    bo.add_argument("--start", default="", help="where to start, e.g. 10:00 (default: middle of the video)")
    rm = sub.add_parser("remove", help="delete videos from the site")
    rm.add_argument("videos", nargs="+")
    sv = sub.add_parser("serve", help="on-demand server for any video (run on your own computer)")
    sv.add_argument("--port", type=int, default=8787); sv.add_argument("--lang", default="en")
    sv.add_argument("--no-push", action="store_true", help="don't publish new pages to GitHub")
    sv.add_argument("--no-tunnel", action="store_true", help="don't start the Cloudflare Tunnel")
    sv.add_argument("--captions", action="store_true", help="skip the transcript panel; use the caption file only (faster)")
    r = sub.add_parser("raw", help="save the raw caption track in all formats to debug/raw/")
    r.add_argument("videos", nargs="+"); r.add_argument("--lang", default="en")
    r.add_argument("--player", action="store_true", help="also save what YouTube's web player loads when CC is on")
    r.add_argument("--show", action="store_true", help="with --player: show the browser window")
    args = ap.parse_args()

    if args.cmd == "raw":
        for v in args.videos:
            vid = video_id(v)
            print(f"{vid}:")
            try:
                dump_raw(vid, args.lang)
            except Exception as e:
                print(f"  direct download failed: {e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}")
            if args.player:
                try:
                    fetch_player_captions(vid, show=args.show, save_raw=True)
                except Exception as e:
                    print(f"  player capture failed: {e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}")
        return

    if args.cmd == "add":
        global DEBUG_DIR
        if args.debug:
            DEBUG_DIR = ROOT / "debug"
        failed = 0
        for v in args.videos:
            vid = video_id(v)
            print(f"{vid}:")
            try:
                get_transcript(vid, args.lang, browser=not args.captions, show=args.show, player=args.player,
                               allow_music=args.allow_music)
            except Exception as e:
                failed += 1
                print(f"  failed: {e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}")
                continue
            if not args.no_correct and acc is not None and acc.available():
                try:
                    run_correction(vid)
                except Exception as e:
                    print(f"  correction skipped: {e.__class__.__name__}: {str(e)[:200]}")
        print(f"Site updated ({len(_load_index())} videos)")
        sys.exit(1 if failed else 0)

    if args.cmd == "paste":
        vid = video_id(args.video)
        text = Path(args.file).read_text(encoding="utf-8") if args.file else read_clipboard()
        segs, chapters = parse_pasted(text)
        if not segs:
            sys.exit("No timestamps found. On YouTube open the transcript, select it all, copy, then run this again."
                     if not args.file else "No timestamps found in that file.")
        print(f"  read {len(segs)} lines, {len(chapters)} chapters from the " + ("file" if args.file else "clipboard"))
        d = {"id": vid, "title": args.title, "channel": args.channel, "published": "", "chapters": chapters}
        if not args.no_meta and not (args.title and args.channel):
            m = fetch_metadata(vid)
            d["title"] = args.title or m["title"]
            d["channel"] = args.channel or m["channel"]
            d["published"] = m.get("published", "")
            d["chapters"] = chapters or m["chapters"]
        for sg in segs:
            sg["label"] = ts(sg["start"])
        d.update(segments=segs, track={"source": "youtube-panel (pasted)"}, source="paste")
        save(d)
        write_page(d)
        write_indexes()

    if args.cmd == "build":
        build()

    if args.cmd == "signin":
        signin()

    if args.cmd == "correct":
        if acc is None or not acc.available():
            sys.exit("Set ELEVENLABS_API_KEY and/or ASSEMBLYAI_API_KEY first (see README), then open a new window.")
        engines = [e.strip() for e in args.engines.split(",") if e.strip()] or None
        for v in args.videos:
            vid = video_id(v)
            print(f"{vid}:")
            try:
                run_correction(vid, "full" if args.full else "selective", engines)
            except Exception as e:
                print(f"  failed: {e.__class__.__name__}: {str(e)[:300]}")
        write_indexes()

    if args.cmd == "usage":
        if acc is None:
            sys.exit("correct.py is missing")
        u = acc._load_usage()
        st = acc.settings()
        m = u.get(acc._month(), {})
        print(f"This month: ${m.get('usd', 0):.3f} of ${st['budget']:.2f} budget")
        for eng, sec in (m.get("seconds") or {}).items():
            print(f"  {eng}: {sec / 60:.1f} minutes of audio")
        for row in (m.get("log") or [])[-15:]:
            print("  " + "  ".join(str(x) for x in row))
        print(f"Engines with keys: {', '.join(acc.available()) or 'none'}; primary: {st['primary']}; "
              f"full re-transcription at {st['full_at']} views")

    if args.cmd == "bakeoff":
        vid = video_id(args.video)
        f = DATA / f"{vid}.json"
        if not f.exists():
            sys.exit(f"Add the video first:  python yousnr.py add {vid}")
        d = json.loads(f.read_text(encoding="utf-8"))
        rep = acc.bakeoff(d, minutes=args.minutes, start=parse_ts(args.start) if args.start else None)
        print(f"{vid}: {ts(rep['from'])}–{ts(rep['to'])}, each engine compared with YouTube's transcript")
        for eng, r in rep["engines"].items():
            print(f"\n  {eng}: {r['differences']} differences in {r['words']} words")
            for lab, yt, heard in r["examples"]:
                print(f"    {lab:>7}  YouTube: {yt!r:<28} {eng}: {heard!r}")
        print(f"\nListen at those timestamps on YouTube to judge which engine is right more often.")

    if args.cmd == "remove":
        import shutil
        index = _load_index()
        for v in args.videos:
            vid = video_id(v)
            (DATA / f"{vid}.json").unlink(missing_ok=True)
            shutil.rmtree(DOCS / vid, ignore_errors=True)
            index.pop(vid, None)
            print(f"removed {vid}")
        INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=0), encoding="utf-8")
        write_indexes(index)

    if args.cmd == "serve":
        serve(args.port, args.lang, push=not args.no_push, tunnel=not args.no_tunnel, browser=not args.captions)


if __name__ == "__main__":
    main()
