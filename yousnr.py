#!/usr/bin/env python3
"""
YouSNR (yousnr.com) — static YouTube-style transcript pages for GitHub Pages.

  python yousnr.py add <url-or-id> [more ...] [--lang en]   fetch from YouTube, save, build
  python yousnr.py paste <url-or-id> [file.txt]             build from a transcript copied off YouTube
                                                             (no file = read the clipboard)
  python yousnr.py build                                    rebuild every page from data/
  python yousnr.py serve [--port 8787]                      on-demand server: any video, fetched on first visit
  python yousnr.py remove <url-or-id> [more ...]            delete videos from the site

Data lives in data/<ID>.json; the site is generated into docs/ (GitHub Pages source).
Run `add` from your own computer: YouTube blocks most cloud/CI IPs, including GitHub Actions.
"""
import argparse, datetime, html, json, os, re, subprocess, sys, threading, time, urllib.parse, urllib.request
from pathlib import Path

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
                        description=vd.get("shortDescription", ""), duration=int(vd.get("lengthSeconds") or 0))
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


def get_transcript(vid, lang="en", browser=False, show=False):
    """Fetch one video end to end and save it. Returns the saved record."""
    chapters, segs, track = [], None, None
    if browser:
        try:
            segs, chapters, track, how = fetch_browser(vid, show=show)
            print(f"  transcript: {how}, {len(segs)} lines")
        except ImportError:
            print("  (browser mode needs:  python -m pip install --user playwright)")
        except Exception as e:
            print(f"  browser method failed: {e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}")
    if not segs:
        global DIRECT_BLOCKED_UNTIL
        try:
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
    d = fetch_metadata(vid)
    d.pop("description", None)
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


def _launch(p, show):
    args = ["--mute-audio", "--autoplay-policy=user-gesture-required"]
    for channel in ("chrome", "msedge", None):        # your installed Chrome, else Edge, else Playwright's
        try:
            return p.chromium.launch(channel=channel, headless=not show, args=args) if channel \
                else p.chromium.launch(headless=not show, args=args)
        except Exception:
            continue
    raise RuntimeError("no browser found: install Chrome, or run  python -m playwright install chromium")


# Find the transcript on screen without relying on YouTube's element names: collect every
# element whose own text is a timestamp, find the smallest container holding most of them
# (that's the transcript list, not the ~20 chapter stamps in the description), read it.
_DOM_JS = r"""() => {
  const re = /^(\d+:)?\d{1,2}:\d{2}$/;
  const stamps = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walk.nextNode());) {
    const el = n.parentElement;
    if (el && re.test(n.nodeValue.trim()) && el.getClientRects().length) stamps.push(el);
  }
  if (stamps.length < 5) return {count: stamps.length, text: ''};
  const counts = new Map();
  for (const st of stamps) for (let e = st; e && e !== document.body; e = e.parentElement) counts.set(e, (counts.get(e) || 0) + 1);
  const need = Math.max(5, Math.floor(stamps.length * 0.7));
  let best = null, bestDepth = -1;
  for (const [e, c] of counts) {
    if (c < need) continue;
    let d = 0; for (let x = e; x; x = x.parentElement) d++;
    if (d > bestDepth) { best = e; bestDepth = d; }
  }
  return {count: stamps.length, text: best ? best.innerText : ''};
}"""

_COUNT_JS = r"""() => {
  const re = /^(\d+:)?\d{1,2}:\d{2}$/; let c = 0;
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walk.nextNode());) if (re.test(n.nodeValue.trim()) && n.parentElement && n.parentElement.getClientRects().length) c++;
  return c;
}"""

_OPEN_JS = r"""() => {
  const p = document.querySelector('ytd-engagement-panel-section-list-renderer[target-id="engagement-panel-searchable-transcript"]');
  if (p) { p.setAttribute('visibility', 'ENGAGEMENT_PANEL_VISIBILITY_EXPANDED'); return true; }
  return false;
}"""


def fetch_browser(vid, show=False):
    from playwright.sync_api import sync_playwright
    replies, log = [], []

    def say(msg):
        log.append(msg)
        print(f"    · {msg}")

    with sync_playwright() as p:
        browser = _launch(p, show)
        say(f"opened {browser.browser_type.name} {browser.version}" + ("" if show else " (hidden)"))
        ctx = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
        if BASE.startswith("https://www.youtube.com"):
            ctx.add_cookies([{"name": "SOCS", "value": "CAI", "domain": ".youtube.com", "path": "/"}])
        page = ctx.new_page()
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

        best, dom, base_stamps = ([], [], ""), None, 0
        try:
            page.goto(f"{BASE}/watch?v={vid}&hl=en", wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("#description, ytd-watch-metadata", timeout=20000)
                say("video page loaded")
            except Exception:
                say("video page loaded, but no description box found")
            page.evaluate("() => { const v = document.querySelector('video'); if (v) v.pause(); }")
            base_stamps = max(count(), 0)

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
        for s in segs:
            s["label"] = ts(s["start"])
    if not segs:
        raise RuntimeError("couldn't open the transcript panel (try --show to watch, --debug to save what happened)")
    track = {"language": label, "generated": "auto-generated" in label.lower() if label else None,
             "source": "youtube-panel"}
    return segs, chapters, track, how


def _parse_panel(resp):
    segs, chapters, label = [], [], ""
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


def fetch_player_captions(vid, show=False):
    from playwright.sync_api import sync_playwright
    got = []

    def say(msg):
        print(f"    · {msg}")

    with sync_playwright() as p:
        browser = _launch(p, show)
        try:
            ctx = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
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
    rows, ci = [], 0
    for s in d["segments"]:
        while ci < len(chapters) and chapters[ci]["start"] <= s["start"]:
            rows.append(f'<h2 class="ch" id="ch{ci}">{e(chapters[ci]["title"])}</h2>')
            ci += 1
        t = int(s["start"])
        rows.append(
            f'<a class="seg" href="https://www.youtube.com/watch?v={vid}&amp;t={t}s" target="_blank" rel="noopener" '
            f'data-s="{s["start"]}" data-d="{s.get("dur") or ""}">'
            f'<span class="ts">{e(s.get("label") or ts(s["start"]))}</span><span class="tx">{e(s["text"])}</span></a>')
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
               .replace("{{ROWS}}", "\n".join(rows)))


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
    lis = "\n".join(
        f'<li><a href="/{d["id"]}/"><span class="t">{html.escape(d.get("title") or d["id"])}</span>'
        f'<span class="c">{html.escape(d.get("channel") or "")}</span></a></li>' for d in items[:200])
    home = (TEMPLATES / "home.html").read_text(encoding="utf-8").replace(
        "{{LIST}}", lis or '<li class="empty">No transcripts yet.</li>')
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


class Fetcher:
    def __init__(self, lang="en"):
        self.lang = lang
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
        if miss and time.time() - miss["at"] < MISSING_RETRY:
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
                d = get_transcript(vid, self.lang)
            except Exception as e:
                name = e.__class__.__name__
                msg = str(e)
                if name in BLOCKED or "429" in msg or "Too Many Requests" in msg:
                    self.cooldown_until = time.time() + BLOCK_COOLDOWN
                    print(f"  YouTube says slow down ({name}); pausing new fetches for {BLOCK_COOLDOWN // 60} min")
                    return 503, "application/json", json.dumps({"error": "busy", "retry": BLOCK_COOLDOWN})
                reason = "no_captions" if name in NO_TRANSCRIPT else "unavailable" if name in UNAVAILABLE else None
                if reason:
                    self.missing[vid] = {"reason": reason, "at": time.time()}
                    MISSING.write_text(json.dumps(self.missing), encoding="utf-8")
                    return 404, "application/json", json.dumps({"error": reason})
                print(f"  failed: {name}: {msg.splitlines()[0] if msg else ''}")
                return 502, "application/json", json.dumps({"error": "failed"})
            self.pending.append(vid)
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


def serve(port=8787, lang="en", push=True, push_every=PUSH_EVERY, host="127.0.0.1"):
    import http.server
    fetcher = Fetcher(lang)
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

        def send_error(self, code, message=None, explain=None):
            if code == 404:
                return self._send(404, "text/html; charset=utf-8", (DOCS / "404.html").read_text(encoding="utf-8"))
            return super().send_error(code, message, explain)

    if push:
        threading.Thread(target=publisher, args=(fetcher, push_every), daemon=True).start()
    srv = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f"YouSNR server on http://localhost:{port}")
    print(f"  try:  http://localhost:{port}/watch?v=265-EZJ_aJE")
    print(f"  public via Cloudflare Tunnel: api.yousnr.com -> http://localhost:{port}")
    print("  Ctrl+C to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("videos", nargs="+"); a.add_argument("--lang", default="en")
    a.add_argument("--debug", action="store_true", help="save YouTube's replies and a screenshot to debug/")
    a.add_argument("--browser", action="store_true", help="read YouTube's panel through a hidden browser first")
    a.add_argument("--show", action="store_true", help="with --browser: show the browser window")
    p = sub.add_parser("paste"); p.add_argument("video"); p.add_argument("file", nargs="?", help="omit to read the clipboard")
    p.add_argument("--title", default=""); p.add_argument("--channel", default="")
    p.add_argument("--no-meta", action="store_true", help="don't look up title/channel online")
    sub.add_parser("build")
    rm = sub.add_parser("remove", help="delete videos from the site")
    rm.add_argument("videos", nargs="+")
    sv = sub.add_parser("serve", help="on-demand server for any video (run on your own computer)")
    sv.add_argument("--port", type=int, default=8787); sv.add_argument("--lang", default="en")
    sv.add_argument("--no-push", action="store_true", help="don't publish new pages to GitHub")
    r = sub.add_parser("raw", help="save the raw caption track in all formats to debug/raw/")
    r.add_argument("videos", nargs="+"); r.add_argument("--lang", default="en")
    args = ap.parse_args()

    if args.cmd == "raw":
        for v in args.videos:
            vid = video_id(v)
            print(f"{vid}:")
            try:
                dump_raw(vid, args.lang)
            except Exception as e:
                print(f"  failed: {e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}")
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
                get_transcript(vid, args.lang, browser=args.browser, show=args.show)
            except Exception as e:
                failed += 1
                print(f"  failed: {e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}")
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
        serve(args.port, args.lang, push=not args.no_push)


if __name__ == "__main__":
    main()
