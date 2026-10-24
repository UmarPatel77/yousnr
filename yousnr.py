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
  python yousnr.py upgrade [--dry-run]                      bring older videos up to the current checking rules
  python yousnr.py autostart on|off                         always on: start with Windows, no window
  python yousnr.py start | stop | status                    turn the background server on / off / check it
  python yousnr.py check-youtube [video]                    can this computer reach YouTube? (cloud servers)

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

NOWIN = 0x08000000 if os.name == "nt" else 0      # CREATE_NO_WINDOW: helper programs never flash a black window
RUN = ROOT / "run"                                # process IDs and the stop signal for background mode (not committed)
LOG = ROOT / "yousnr.log"                         # what background mode would have printed


def write_atomic(path, text):
    """Write a file so a reader (or a sudden power-off) never sees half of it."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # Windows: another thread is reading it this instant
            time.sleep(0.05 * (i + 1))
    path.write_text(text, encoding="utf-8")
    tmp.unlink(missing_ok=True)


class LogFile:
    """Stands in for the screen when YouSNR runs in the background: every line goes to yousnr.log
    with the date and time. The file is opened and closed for each line, so you can open it in
    Notepad any time, and it's renamed to yousnr.log.old when it passes 5 MB."""
    closed = False
    encoding = "utf-8"

    def __init__(self, path=LOG, max_bytes=5_000_000):
        self.path, self.max, self.buf, self.lock = Path(path), max_bytes, "", threading.Lock()

    def write(self, s):
        with self.lock:
            self.buf += s
            if "\n" not in self.buf:
                return len(s)
            *lines, self.buf = self.buf.split("\n")
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            text = "".join(f"{stamp}  {l.rstrip()}\n" if l.strip() else "\n" for l in lines)
            try:
                if self.path.exists() and self.path.stat().st_size > self.max:
                    os.replace(self.path, self.path.with_name(self.path.name + ".old"))
            except OSError:
                pass
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(text)
            except OSError:
                pass
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        import io
        raise io.UnsupportedOperation("fileno")


def use_log_file(path=LOG):
    sys.stdout = sys.stderr = LogFile(path)


def load_windows_user_env():
    """Keys saved with `setx` live in the registry. A window opened earlier, or a program started by
    Windows at sign-in, may not have them yet, so read them from there when they're missing."""
    if os.name != "nt":
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            i = 0
            while True:
                try:
                    name, value, kind = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                if (name.upper().startswith("YOUSNR_") or name.upper().endswith("_API_KEY")) and name not in os.environ:
                    os.environ[name] = os.path.expandvars(value) if kind == winreg.REG_EXPAND_SZ else str(value)
    except Exception:
        pass


def load_env_file():
    """On a Linux server the keys live in ~/.config/yousnr.env (private, outside the project); the service
    reads it, and so do commands you type, like `status` or `correct`."""
    f = Path(os.environ.get("YOUSNR_ENV_FILE") or Path.home() / ".config" / "yousnr.env")
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


def read_pid(f):
    try:
        return int(Path(f).read_text(encoding="utf-8").split()[0])
    except Exception:
        return None


def process_name(pid):
    """What's running under this process ID ('python.exe', 'cloudflared.exe', a command line on Linux), or None."""
    if not pid:
        return None
    if os.name == "nt":
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=20, creationflags=NOWIN)
        except Exception:
            return None
        m = re.match(r'"([^"]+)","(\d+)"', r.stdout.strip())
        return m.group(1).lower() if m and int(m.group(2)) == pid else None
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        return cmd or None
    except Exception:
        return None


def alive(pidfile, looks_like):
    """The process ID in pidfile, if that process is still running and is the kind we expect."""
    pid = read_pid(pidfile)
    name = process_name(pid)
    return pid if name and any(w in name for w in looks_like) else None


def kill(pid, tree=False):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"] + (["/T"] if tree else []),
                       capture_output=True, timeout=30, creationflags=NOWIN)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


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


def yt_proxy():
    """YOUSNR_PROXY sends only the YouTube traffic through a proxy, e.g. Cloudflare WARP on a cloud
    server (socks5://127.0.0.1:40000), because YouTube blocks data-centre addresses. The speech
    APIs, GitHub and the Cloudflare Tunnel still go direct. Empty (the default) = no proxy."""
    return os.environ.get("YOUSNR_PROXY", "").strip()


def yt_ipv4():
    """YOUSNR_PROXY_IPV4=1: reach YouTube over IPv4 through the proxy (WARP's IPv4 and IPv6 exits can be
    treated differently by YouTube; `check-youtube` shows which works)."""
    return os.environ.get("YOUSNR_PROXY_IPV4", "").strip().lower() in ("1", "true", "yes", "on")


def yt_requests_proxies():
    p = yt_proxy()
    if p and not yt_ipv4():
        p = p.replace("socks5://", "socks5h://", 1)           # socks5h: the proxy looks up names too
    return {"http": p, "https": p} if p else {}             # socks5: names looked up here, IPv4 only


def yt_dlp_net_opts():
    """Network options for yt-dlp: the proxy, and IPv4 if asked."""
    o = {}
    if yt_proxy():
        o["proxy"] = yt_proxy()
    if yt_ipv4():
        o["source_address"] = "0.0.0.0"
    return o


def transcript_api():
    from youtube_transcript_api import YouTubeTranscriptApi
    px = yt_requests_proxies()
    if not px:
        return YouTubeTranscriptApi()
    from youtube_transcript_api.proxies import GenericProxyConfig
    return YouTubeTranscriptApi(proxy_config=GenericProxyConfig(http_url=px["http"], https_url=px["https"]))


def _json_after(page, marker):
    i = page.find(marker)
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(page, page.index("{", i))[0]
    except ValueError:
        return None


def meta_from_player(pr):
    """Title, channel, date, length, category and music flag from YouTube's player data."""
    vd = (pr or {}).get("videoDetails") or {}
    if not vd.get("title"):
        return {}
    mf = ((pr or {}).get("microformat") or {}).get("playerMicroformatRenderer") or {}
    m = dict(title=vd["title"], channel=vd.get("author", ""),
             published=(mf.get("publishDate") or mf.get("uploadDate") or "")[:10].replace("-", ""),
             description=vd.get("shortDescription", ""), duration=int(vd.get("lengthSeconds") or 0),
             category=mf.get("category", ""), music_type=vd.get("musicVideoType", ""))
    m["chapters"] = chapters_from_description(m["description"])
    return m


BROWSER_META = {}     # filled by fetch_browser: what the real browser saw, for when plain requests are blocked


def fetch_metadata(vid):
    """Title, channel, date, chapters. One plain request to the watch page; yt-dlp only if that fails.
    Where YouTube blocks plain requests (cloud servers), the browser fills these in later."""
    meta = {"id": vid, "title": "", "channel": "", "published": "", "description": "", "chapters": []}
    bot_check = False
    try:
        import requests
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        sess.cookies.set("SOCS", "CAI", domain=".youtube.com")
        sess.proxies.update(yt_requests_proxies())
        page = sess.get(f"https://www.youtube.com/watch?v={vid}&hl=en", timeout=20).text
        m = meta_from_player(_json_after(page, "ytInitialPlayerResponse = ") or {})
        if m:
            meta.update(m)
            return meta
        bot_check = "confirm you" in page and "not a bot" in page
    except Exception:
        pass
    try:
        if bot_check:                 # yt-dlp would get the same "not a bot" page: skip straight to oEmbed
            raise RuntimeError("YouTube asked to confirm we're not a bot")
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, **yt_dlp_net_opts()}
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
            import requests
            url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
                f"https://www.youtube.com/watch?v={vid}")
            o = requests.get(url, timeout=15, proxies=yt_requests_proxies()).json()
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


def music_why(meta):
    if meta.get("music_type"):
        return f"YouTube flags it as {meta['music_type']}"
    if (meta.get("category") or "").lower() == "music":
        return "category: Music"
    return "a '- Topic' music channel"


def get_transcript(vid, lang="en", browser=True, show=False, player=False, allow_music=False):
    """Fetch one video end to end and save it. Returns the saved record."""
    meta = fetch_metadata(vid)
    if not allow_music and is_music(meta):
        raise MusicVideo(f"music video, skipped ({music_why(meta)})")
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
    seen = BROWSER_META.pop(vid, None)        # what the browser saw fills gaps a blocked plain request left
    if seen:
        for k, v in seen.items():
            if v and not meta.get(k):
                meta[k] = v
        if not allow_music and is_music(meta):
            raise MusicVideo(f"music video, skipped ({music_why(meta)})")
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
    if yt_proxy():                                   # Playwright wants the login apart from the address
        u = urllib.parse.urlsplit(yt_proxy())
        common["proxy"] = {"server": f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")}
        if u.username:
            common["proxy"].update(username=urllib.parse.unquote(u.username),
                                   password=urllib.parse.unquote(u.password or ""))
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
            try:
                m = meta_from_player(page.evaluate(
                    "() => { const p = window.ytInitialPlayerResponse;"
                    " return p ? {videoDetails: p.videoDetails || null, microformat: p.microformat || null} : null; }"))
                if m:
                    BROWSER_META[vid] = m
            except Exception:
                pass
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
    tl = transcript_api().list(vid)
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
    tl = transcript_api().list(vid)
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
            return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=10, creationflags=NOWIN).stdout
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
    checking = not d.get("corrected") and acc is not None and bool(acc.available())   # word check still to come

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
                                         if nfix else CHECKING_NOTE if checking else ""))
               .replace("{{FIXCLASS}}", "" if nfix else "hidden")
               .replace("{{ROWS}}", "\n".join(rows)))


def index_entry(d):
    e = {"title": d.get("title", ""), "channel": d.get("channel", ""), "added": d.get("added", "")}
    if acc is not None:
        e.update(v=acc.version_of(d), bleeps=acc.bleep_count(d), dur=d.get("duration") or 0)
    return e


def ensure_index_fields():
    """One-time: fill in rule version / bleep count for videos saved before the upgrade queue existed."""
    if acc is None:
        return _load_index()
    index, changed = _load_index(), 0
    for vid, e in index.items():
        if "v" in e and "bleeps" in e:
            continue
        f = DATA / f"{vid}.json"
        if f.exists():
            index[vid] = dict(e, **{k: v for k, v in index_entry(json.loads(f.read_text(encoding="utf-8"))).items()
                                   if k in ("v", "bleeps", "dur")})
            changed += 1
    if changed:
        with _save_lock:                       # re-read, so a video saved meanwhile isn't dropped
            now = _load_index()
            for vid, e in index.items():
                if vid in now:
                    now[vid] = dict(now[vid], **{k: e[k] for k in ("v", "bleeps", "dur") if k in e})
            write_atomic(INDEX, json.dumps(now, ensure_ascii=False, indent=0))
            index = now
    return index


def run_upgrade(vid, log=print):
    """Bring one video up to the current checking rules, paying only for what the new rules need.
    Returns 'stamped' (nothing to pay for), the correction summary, or None."""
    if acc is None:
        return None
    f = DATA / f"{vid}.json"
    if not f.exists():
        return None
    d = json.loads(f.read_text(encoding="utf-8"))
    entry = {"v": acc.version_of(d), "bleeps": acc.bleep_count(d)}
    rules = acc.pending(entry)
    if not rules:
        if entry["v"] < acc.CURRENT and d.get("corrected"):
            d["corrected"]["version"] = acc.CURRENT
            relaid = acc.relayout(d)                   # free: saved fixes laid out with the current rules
            save(d, quiet=True)
            if relaid:
                write_page(d)
                log(f"  {vid}: fixes laid out again (free)")
                return "relaid"
            return "stamped"
        return None
    if not acc.available():
        return None
    reasons = acc.reasons_for(rules)
    log(f"  upgrade: {'; '.join(r['what'] for r in rules)}")
    r = acc.correct(d, mode="selective", meta=d, log=log, only_reasons=reasons if d.get("corrected") else None)
    save(d, quiet=True)
    write_page(d)
    log(f"  upgraded to rules v{acc.CURRENT}: {r['fixes']} fix(es) in total; this run cost "
        f"${acc.spent_this_month():.2f} of ${acc.settings()['budget']:.2f} used this month")
    return r


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
CHECKING_NOTE = ('<span class="fixnote"> · double-checking words with speech recognition; '
                 'reload in a few minutes for the corrected version</span>')


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
        index[d["id"]] = index_entry(d)
    DATA.mkdir(exist_ok=True)
    write_atomic(INDEX, json.dumps(index, ensure_ascii=False, indent=0))
    n = write_indexes(index)
    print(f"Built {n} page(s) into docs/")


_save_lock = threading.Lock()


def save(d, quiet=False):
    DATA.mkdir(exist_ok=True)
    f = DATA / f"{d['id']}.json"
    if f.exists():
        d.setdefault("added", json.loads(f.read_text(encoding="utf-8")).get("added"))
    if not d.get("added"):
        d["added"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    write_atomic(f, json.dumps(d, ensure_ascii=False, indent=1))
    with _save_lock:
        index = _load_index()
        index[d["id"]] = index_entry(d)
        write_atomic(INDEX, json.dumps(index, ensure_ascii=False, indent=0))
    if not quiet:
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
    """Background worker. In order of priority:
      1. new videos: selective check
      2. videos someone is reading that were checked with older rules: upgrade, jumps the queue
      3. popular videos: full re-transcription
      4. idle time: upgrade older videos a few per hour, most-viewed first
    Never goes over the monthly budget; when it's used up, background upgrades wait for next month."""
    VIEWS = DATA / "_views.json"

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.jobs, self.queued = [], set()
        self.cv = threading.Condition()
        self.failed, self.done_this_hour, self.hour = {}, 0, int(time.time() // 3600)
        self.paused_until = 0.0
        self.stopping, self.busy = False, None
        try:
            self.views = json.loads(self.VIEWS.read_text(encoding="utf-8"))
        except Exception:
            self.views = {}
        self.enabled = acc is not None and bool(acc.available())
        self.per_hour = int(os.environ.get("YOUSNR_UPGRADES_PER_HOUR", "6"))
        if self.enabled:
            threading.Thread(target=self.loop, daemon=True).start()
            threading.Thread(target=self.backfill, daemon=True).start()

    def enqueue(self, vid, mode, front=False):
        if not self.enabled or (vid, mode) in self.queued:
            return
        with self.cv:
            self.queued.add((vid, mode))
            (self.jobs.insert(0, (vid, mode)) if front else self.jobs.append((vid, mode)))
            self.cv.notify()

    def viewed(self, vid):
        f = DATA / f"{vid}.json"
        if not f.exists():
            return
        n = self.views[vid] = self.views.get(vid, 0) + 1
        write_atomic(self.VIEWS, json.dumps(self.views))
        if not self.enabled or time.time() < self.paused_until:
            return
        e = _load_index().get(vid) or {}
        if e and acc.pending(e) and self.failed.get(vid, 0) < time.time() - 86400:
            self.enqueue(vid, "upgrade", front=True)          # someone's reading an outdated page
        if n >= acc.settings()["full_at"]:
            try:
                mode = (json.loads(f.read_text(encoding="utf-8")).get("corrected") or {}).get("mode")
            except Exception:
                mode = None
            if mode != "full":
                self.enqueue(vid, "full")

    def backfill(self):
        time.sleep(20)
        ensure_index_fields()
        while True:
            try:
                self._backfill_once()
            except Exception as e:
                print(f"  upgrade scan failed: {e.__class__.__name__}: {e}")
            time.sleep(60)

    def _backfill_once(self):
        hour = int(time.time() // 3600)
        if hour != self.hour:
            self.hour, self.done_this_hour = hour, 0
        if self.jobs or time.time() < self.paused_until:
            return
        index = _load_index()
        stamped = 0
        for vid, e in index.items():                          # free: rule changes that don't affect a video
            if e.get("v", 0) < acc.CURRENT and e.get("v", 0) > 0 and not acc.pending(e) and stamped < 200:
                r = run_upgrade(vid)
                if r in ("stamped", "relaid"):
                    stamped += 1
                if r == "relaid":
                    self.fetcher.pending.append(vid)
        if self.done_this_hour >= self.per_hour:
            return
        todo = [vid for vid, e in index.items() if acc.pending(e)
                and self.failed.get(vid, 0) < time.time() - 86400 and (vid, "upgrade") not in self.queued]
        if todo:
            todo.sort(key=lambda v: (-self.views.get(v, 0), index[v].get("added") or ""), reverse=False)
            self.done_this_hour += 1
            self.enqueue(todo[0], "upgrade")

    def finish(self, timeout):
        """Turning off: start nothing new, and give a paid check that's under way time to finish."""
        with self.cv:
            self.stopping = True
            self.cv.notify_all()
        if self.busy:
            print(f"  finishing the word check on {self.busy} first (up to {timeout // 60} min)…")
        end = time.time() + timeout
        while self.busy and time.time() < end:
            time.sleep(0.5)

    def loop(self):
        while True:
            with self.cv:
                while not self.jobs and not self.stopping:
                    self.cv.wait()
                if self.stopping:
                    return
                vid, mode = self.jobs.pop(0)
                self.busy = vid
            try:
                if time.time() < self.paused_until:
                    continue
                print(f"{time.strftime('%H:%M:%S')}  {vid}  checking words ({mode})")
                r = run_upgrade(vid, log=print) if mode == "upgrade" else run_correction(vid, mode, log=print)
                if r and r != "stamped":
                    self.fetcher.pending.append(vid)            # "relaid" or a real correction
            except Exception as e:
                if e.__class__.__name__ == "OverBudget":
                    now = datetime.date.today()
                    nxt = datetime.date(now.year + (now.month == 12), now.month % 12 + 1, 1)
                    self.paused_until = time.mktime(nxt.timetuple())
                    print(f"  monthly budget reached: corrections pause until {nxt} ({e})")
                else:
                    self.failed[vid] = time.time()
                    print(f"  correction skipped: {e.__class__.__name__}: {str(e)[:200]}")
            finally:
                self.queued.discard((vid, mode))
                self.busy = None


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
                    write_atomic(MISSING, json.dumps(self.missing))
                    return 404, "application/json", json.dumps({"error": reason})
                print(f"  failed: {name}: {msg.splitlines()[0] if msg else ''}")
                return 502, "application/json", json.dumps({"error": "failed"})
            self.pending.append(vid)
            if getattr(self, "corrector", None):
                self.corrector.enqueue(vid, "selective")
            return 200, "text/html; charset=utf-8", render_page(d)
        finally:
            self.lock.release()


_publish_lock = threading.Lock()


def publish(fetcher, quiet_if_none=True):
    """Commit and push the pages made since the last push. Returns True if nothing is left waiting."""
    with _publish_lock:
        if not fetcher.pending:
            return True
        ids, fetcher.pending = fetcher.pending[:], []
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")

        def git(*a):
            return subprocess.run(["git", *a], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
                                  creationflags=NOWIN)
        lock = ROOT / ".git" / "index.lock"           # left behind if the computer lost power mid-commit
        if lock.exists() and time.time() - lock.stat().st_mtime > 600:
            lock.unlink(missing_ok=True)
        try:
            git("add", "-A", "data", "docs")
            c = git("commit", "-m", f"add {len(ids)} transcript(s): {' '.join(ids[:10])}")
            if c.returncode != 0 and "nothing to commit" in (c.stdout + c.stderr):
                return True
            r = git("push")
            if r.returncode != 0 and re.search(r"rejected|fetch first|non-fast-forward", r.stderr + r.stdout):
                m = git("pull", "--no-rebase", "--no-edit")      # someone else pushed (a code update): merge, retry
                if m.returncode == 0:
                    r = git("push")
                else:
                    git("merge", "--abort")
        except subprocess.TimeoutExpired:
            r = None
        if r is not None and r.returncode == 0:
            print(f"  published {len(ids)} new page(s) to GitHub")
            return True
        err = "timed out" if r is None else (r.stderr or r.stdout).strip().splitlines()[-1:]
        print(f"  git push failed (will retry): {err}")
        fetcher.pending = ids + fetcher.pending
        return False


def publisher(fetcher, every):
    """Commit and push new pages to GitHub in batches, so they become static and instant."""
    if not (ROOT / ".git").exists():
        print("  (not a git repo: new pages won't be published to GitHub)")
        return
    last = 0.0
    while True:
        time.sleep(15)
        if not fetcher.pending or time.time() - last < every:
            continue
        last = time.time()
        if not publish(fetcher):
            last = time.time() - every + 60       # retry in about a minute


TUNNEL_TOKEN_FILE = ROOT / "tunnel-token.txt"   # never commit this (it's in .gitignore)


TUNNEL = {"state": "off"}                       # shown by `status`


class Tunnel:
    """Runs cloudflared next to the server and restarts it if it ever stops."""
    PID = RUN / "tunnel.pid"

    def __init__(self):
        self.proc, self.stopping = None, threading.Event()

    def start(self):
        import shutil
        if not TUNNEL_TOKEN_FILE.exists():
            print("  tunnel: off (no tunnel-token.txt), so only this computer can reach the server")
            return self
        token = TUNNEL_TOKEN_FILE.read_text(encoding="utf-8").strip().split()[-1]   # tolerate a pasted full command
        exe = (os.environ.get("YOUSNR_CLOUDFLARED") or shutil.which("cloudflared")
               or next((c for c in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
                                    r"C:\Program Files\cloudflared\cloudflared.exe") if Path(c).exists()), None))
        if not exe:
            print("  tunnel: cloudflared isn't installed. Run:  winget install --id Cloudflare.cloudflared")
            return self
        old = alive(self.PID, ("cloudflared",))       # left over from a crash or power cut
        if old:
            kill(old)
        TUNNEL["state"] = "connecting"
        threading.Thread(target=self._run, args=(exe, token), daemon=True).start()
        print("  tunnel: connecting…")
        return self

    def _run(self, exe, token):
        fails, first = 0, True
        while not self.stopping.is_set():
            t0, connected, rejected = time.time(), False, False
            RUN.mkdir(exist_ok=True)
            self.proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "run", "--token", token],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
            self.PID.write_text(str(self.proc.pid), encoding="utf-8")
            with (ROOT / "tunnel.log").open("w" if first else "a", encoding="utf-8") as log:
                first = False
                for line in self.proc.stderr:
                    log.write(line)
                    log.flush()
                    if "Registered tunnel connection" in line and not connected:
                        connected = True
                        TUNNEL["state"] = "connected"
                        print("  tunnel: connected, api.yousnr.com is live")
                    elif " ERR " in line and "Unauthorized" in line:
                        rejected = True
            self.proc.wait()
            self.PID.unlink(missing_ok=True)
            if self.stopping.is_set():
                break
            if rejected:
                TUNNEL["state"] = "token rejected"
                print("  tunnel: the token in tunnel-token.txt was rejected; copy it again from Cloudflare")
                break
            fails = fails + 1 if time.time() - t0 < 300 else 0
            wait = min(600, 15 * 2 ** max(0, min(fails, 6) - 1))
            TUNNEL["state"] = "restarting"
            print(f"  tunnel: stopped (see tunnel.log); starting it again in {wait} s")
            self.stopping.wait(wait)
        TUNNEL["state"] = "off"

    def stop(self):
        self.stopping.set()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
        self.PID.unlink(missing_ok=True)


def keep_awake(stop):
    """Windows: while plugged in, don't let the computer fall asleep on its own while YouSNR is on.
    On battery Windows sleeps and hibernates as usual, so a laptop never runs itself flat. The screen can
    still turn off, and choosing Sleep or Hibernate yourself always works. YOUSNR_KEEP_AWAKE=0 turns this off.
    Runs as its own thread (Windows ties the request to the thread that made it)."""
    if os.name != "nt" or os.environ.get("YOUSNR_KEEP_AWAKE", "1") == "0":
        return
    try:
        import ctypes

        class PowerStatus(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        k32 = ctypes.windll.kernel32
        setstate = k32.SetThreadExecutionState
        setstate.argtypes, setstate.restype = [ctypes.c_uint32], ctypes.c_uint32
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    except Exception:
        return
    awake = None
    while not stop.is_set():
        ps = PowerStatus()
        plugged = not k32.GetSystemPowerStatus(ctypes.byref(ps)) or ps.ACLineStatus != 0   # 255 = desktop/unknown
        if plugged != awake:
            setstate(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if plugged else 0))
            print("  plugged in: keeping this computer from sleeping on its own" if plugged else
                  "  on battery: Windows can sleep or hibernate as usual (YouSNR pauses while it does)")
            awake = plugged
        stop.wait(60)
    setstate(ES_CONTINUOUS)


SERVER_PID = RUN / "server.pid"
BG_PID = RUN / "background.pid"
STOP = RUN / "stop"                  # `python yousnr.py stop` creates this; the server sees it and shuts down cleanly
PY_NAMES = ("python", "yousnr")      # what our own processes look like in the task list
EXIT_ALREADY_RUNNING, EXIT_TURNED_OFF = 3, 4


def serve(port=8787, lang="en", push=True, push_every=PUSH_EVERY, host="127.0.0.1", tunnel=True, browser=True):
    import http.server

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = os.name != "nt"      # on Windows that would let two servers share the port

    srv = Server((host, port), None, bind_and_activate=False)
    try:                                            # claim the port first: only one YouSNR at a time
        srv.server_bind()
        srv.server_activate()
    except OSError:
        srv.server_close()
        if alive(BG_PID, PY_NAMES):
            print("YouSNR is already running in the background, so there's nothing to start.\n"
                  "  python yousnr.py status    see what it's doing\n"
                  "  python yousnr.py stop      turn it off (then this window version can run)")
        else:
            print(f"Port {port} is already in use: YouSNR is probably already running in another window.")
        sys.exit(EXIT_ALREADY_RUNNING)
    RUN.mkdir(exist_ok=True)
    STOP.unlink(missing_ok=True)
    background = isinstance(sys.stdout, LogFile)
    how = "service" if os.environ.get("INVOCATION_ID") else "background" if background else "window"
    SERVER_PID.write_text(f"{os.getpid()} {how}", encoding="utf-8")
    started = time.time()

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
                    {"ok": True, "videos": len(_load_index()), "paused_for": max(0, int(fetcher.cooldown_until - time.time())),
                     "tunnel": TUNNEL["state"], "up": int(time.time() - started)}))
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

    srv.RequestHandlerClass = Handler
    corrector = fetcher.corrector = Corrector(fetcher)
    if push:
        threading.Thread(target=publisher, args=(fetcher, push_every), daemon=True).start()
    print("YouSNR is ON" + {"service": "  (as a service)", "background": "  (in the background)"}.get(
        how, "   (close this window to turn it off)"))
    print(f"  local:  http://localhost:{port}/watch?v=265-EZJ_aJE")
    if corrector.enabled:
        st = acc.settings()
        print(f"  corrections: on ({', '.join(acc.available())}); budget ${acc.spent_this_month():.2f} of "
              f"${st['budget']:.2f} used this month; full re-transcription at {st['full_at']} views")
    else:
        print("  corrections: off (set ELEVENLABS_API_KEY / ASSEMBLYAI_API_KEY to turn on)")
    awake_stop = threading.Event()
    threading.Thread(target=keep_awake, args=(awake_stop,), daemon=True).start()
    tun = Tunnel().start() if tunnel else None
    print()

    asked_to_stop = threading.Event()
    try:                                       # `systemctl stop` on a server: finish up like `stop` does
        import signal
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    except Exception:
        pass

    def watch_for_stop():
        while not STOP.exists():
            time.sleep(1)
        asked_to_stop.set()
        srv.shutdown()
    threading.Thread(target=watch_for_stop, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("turning off…")
        srv.server_close()
        if corrector.enabled:
            corrector.finish(150)
        if push and fetcher.pending and (ROOT / ".git").exists():
            print(f"  publishing {len(fetcher.pending)} waiting page(s) first")
            publish(fetcher)
        if tun:
            tun.stop()
        awake_stop.set()
        if read_pid(SERVER_PID) == os.getpid():
            SERVER_PID.unlink(missing_ok=True)
        STOP.unlink(missing_ok=True)
        print("YouSNR is OFF")
    if asked_to_stop.is_set():
        sys.exit(EXIT_TURNED_OFF)


# ---------------------------------------------------------------- always on
# `autostart on` puts a shortcut in Windows' Startup folder. At sign-in it runs the keeper
# (`background`) with no window. The keeper starts the server hidden, writes everything to
# yousnr.log, and starts the server again if it ever stops. `stop` / `start` / `status` control it.

def python_for(windowless):
    """pythonw.exe runs with no window at all; python.exe started with NOWIN gets an invisible console
    that everything it starts (git, ffmpeg, cloudflared, the browser driver) shares, so nothing pops up."""
    exe = Path(sys.executable)
    if os.name != "nt":
        return str(exe)
    other = exe.with_name("pythonw.exe" if windowless else "python.exe")
    return str(other) if other.exists() else str(exe)


def health(port=8787, url=None, timeout=3):
    try:
        if url:
            opener = urllib.request.build_opener()
        else:
            url, opener = f"http://127.0.0.1:{port}/api/health", urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "yousnr-status"}), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def startup_folder():
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 7, None, 0, buf)      # 7 = the Startup folder
        if buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_on():
    return os.name == "nt" and (startup_folder() / "YouSNR.lnk").exists()


def run_background(port=8787):
    """The keeper: what the sign-in shortcut runs. No window; restarts the server if it stops."""
    use_log_file()
    RUN.mkdir(exist_ok=True)
    other = alive(BG_PID, PY_NAMES)
    if other and other != os.getpid():
        return
    BG_PID.write_text(str(os.getpid()), encoding="utf-8")
    py = python_for(windowless=False)
    fails, noted = 0, False

    def copy(stream):
        for line in stream:
            sys.stdout.write("  " + line)
    try:
        print(f"keeper started (process {os.getpid()})")
        while True:
            t0 = time.time()
            p = subprocess.Popen([py, "-X", "utf8", str(ROOT / "yousnr.py"), "serve", "--port", str(port), "--log", str(LOG)],
                                 cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
            threading.Thread(target=copy, args=(p.stderr,), daemon=True).start()
            code = p.wait()
            if code == EXIT_TURNED_OFF or read_pid(BG_PID) != os.getpid():
                break
            if code == EXIT_ALREADY_RUNNING:
                if not noted:
                    print("another YouSNR is already running (in a window?); taking over when it closes")
                    noted = True
                time.sleep(60)
                continue
            noted = False
            fails = fails + 1 if time.time() - t0 < 600 else 0
            wait = min(300, 10 * 2 ** max(0, min(fails, 6) - 1))       # 10 s, then longer if it keeps failing
            print(f"the server stopped unexpectedly (exit code {code}); starting it again in {wait} s")
            time.sleep(wait)
    finally:
        if read_pid(BG_PID) == os.getpid():
            BG_PID.unlink(missing_ok=True)
        print("keeper stopped")


SERVICE = Path("/etc/systemd/system/yousnr.service")      # installed by deploy/oracle-setup.sh


def as_service():
    return os.name != "nt" and SERVICE.exists()


def systemctl(*a):
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", *a], capture_output=True, text=True, timeout=300)
    except Exception as e:
        print(f"  couldn't run systemctl: {e}")
        return False
    if r.returncode != 0 and (r.stderr or r.stdout).strip():
        print("  " + (r.stderr or r.stdout).strip().splitlines()[-1])
    return r.returncode == 0


def start_background(port=8787):
    if as_service():
        if health(port):
            print("YouSNR is already on.  (yousnr status)")
            return True
        print("Turning YouSNR on…", flush=True)
        systemctl("start", "yousnr")
        for _ in range(90):
            time.sleep(1)
            if health(port):
                print("YouSNR is ON.")
                return True
        print("It's still starting. Check in a minute with:  yousnr status")
        return False
    if health(port) or alive(BG_PID, PY_NAMES) or alive(SERVER_PID, PY_NAMES):
        print("YouSNR is already on.  (python yousnr.py status)")
        return True
    RUN.mkdir(exist_ok=True)
    STOP.unlink(missing_ok=True)
    cmd = [python_for(windowless=True), str(ROOT / "yousnr.py"), "background", "--port", str(port)]
    kw = dict(cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200                  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:                                              # + BREAKAWAY_FROM_JOB: keeps running after this window closes
            subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kw)
        except OSError:
            subprocess.Popen(cmd, creationflags=flags, **kw)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kw)
    print("Turning YouSNR on in the background…", flush=True)
    for _ in range(90):
        time.sleep(1)
        if health(port):
            print("YouSNR is ON. No window needed; you can close this one.")
            print("  python yousnr.py status    what it's doing      python yousnr.py stop    turn it off")
            return True
    print("It's still starting. Check in a minute with:  python yousnr.py status   (details in yousnr.log)")
    return False


def stop_background(wait=240):
    if as_service():
        print("Turning YouSNR off (it finishes what it's doing first)…", flush=True)
        systemctl("stop", "yousnr")
        LogFile().write("turned off with `yousnr stop`\n")
        print("YouSNR is OFF.  Turn it on again with:  yousnr start")
        if systemctl("is-enabled", "--quiet", "yousnr"):
            print("  It turns on again when the server restarts. To stop that too:  yousnr autostart off")
        return
    keeper = alive(BG_PID, PY_NAMES)
    if keeper:
        kill(keeper)                  # the keeper only restarts the server; it has nothing to save
    BG_PID.unlink(missing_ok=True)
    server = alive(SERVER_PID, PY_NAMES)
    if server:
        RUN.mkdir(exist_ok=True)
        STOP.write_text("stop", encoding="utf-8")
        print("Turning YouSNR off (it finishes what it's doing first)…", flush=True)
        end = time.time() + wait
        while time.time() < end and process_name(server):
            time.sleep(1)
        if process_name(server):
            kill(server, tree=True)
            print("  it didn't finish in time, so it was stopped")
    tunnel = alive(Tunnel.PID, ("cloudflared",))
    if tunnel:
        kill(tunnel)
    for f in (SERVER_PID, STOP, Tunnel.PID):
        f.unlink(missing_ok=True)
    if keeper or server:
        LogFile().write("turned off with `python yousnr.py stop`\n")
    if not (keeper or server):
        print("YouSNR was already off.")
        return
    print("YouSNR is OFF.")
    print("  Videos already on yousnr.com still work; new ones show \"server offline\" until it's on again.")
    if autostart_on():
        print("  It turns on again the next time you sign in to Windows. To stop that too:  python yousnr.py autostart off")


def _ago(secs):
    secs = int(secs)
    return (f"{secs // 86400} d {secs % 86400 // 3600} h" if secs >= 86400 else
            f"{secs // 3600} h {secs % 3600 // 60} min" if secs >= 3600 else f"{secs // 60} min")


def show_status(port=8787, lines=15):
    keeper, server, h = alive(BG_PID, PY_NAMES), alive(SERVER_PID, PY_NAMES), health(port)
    try:
        how = SERVER_PID.read_text(encoding="utf-8").split()[1]
    except Exception:
        how = "background" if keeper else "window"
    if h:
        print("YouSNR is ON" + {"service": "  (as a service)", "background": "  (in the background)"}.get(how, "  (in a window)")
              + f", for {_ago(h.get('up', 0))}")
        n = h.get("videos", 0)
        print(f"  {n} video{'' if n == 1 else 's'} on the site")
        public = health(url="https://api.yousnr.com/api/health", timeout=10)
        print(f"  tunnel: {h.get('tunnel', '?')}; reachable from the internet: {'yes' if public else 'no'}")
        if h.get("paused_for"):
            print(f"  new videos paused for {max(1, h['paused_for'] // 60)} more min (YouTube asked us to slow down)")
    elif keeper or server:
        print("YouSNR is STARTING (or stuck). If this lasts more than a few minutes, see yousnr.log.")
    else:
        print("YouSNR is OFF")
    if os.name == "nt":
        print(f"  turns on when you sign in to Windows: {'yes' if autostart_on() else 'no'}")
    elif as_service():
        en = subprocess.run(["systemctl", "is-enabled", "yousnr"], capture_output=True, text=True).stdout.strip()
        print(f"  starts when the server starts: {'yes' if en == 'enabled' else 'no'}")
    if yt_proxy():
        print(f"  YouTube traffic goes through {yt_proxy()}  (check-youtube tests it)")
    if acc is not None and acc.available():
        st = acc.settings()
        print(f"  corrections: {', '.join(acc.available())}; ${acc.spent_this_month():.2f} of ${st['budget']:.2f} used this month")
    else:
        print("  corrections: off (no ElevenLabs / AssemblyAI key found)")
    if LOG.exists() and lines:
        tail = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        print(f"\nLast {len(tail)} lines of yousnr.log:")
        for line in tail:
            print("  " + line)


def set_autostart(on):
    if as_service():
        if systemctl("enable" if on else "disable", "yousnr"):
            print("YouSNR now starts by itself whenever the server starts." if on else
                  "YouSNR no longer starts by itself when the server starts.")
        if on:
            start_background()
        return
    if os.name != "nt":
        print("autostart is for Windows. On a Linux server, run deploy/oracle-setup.sh (it sets up a service).")
        return
    lnk = startup_folder() / "YouSNR.lnk"
    if not on:
        lnk.unlink(missing_ok=True)
        print("YouSNR no longer turns on when you sign in to Windows.")
        if health() or alive(BG_PID, PY_NAMES):
            print("  It's still on right now.  python yousnr.py stop  turns it off.")
        return
    env = dict(os.environ, YS_LNK=str(lnk), YS_TARGET=python_for(windowless=True),
               YS_ARGS=f'"{ROOT / "yousnr.py"}" background', YS_DIR=str(ROOT))
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:YS_LNK); $s.TargetPath=$env:YS_TARGET; "
          "$s.Arguments=$env:YS_ARGS; $s.WorkingDirectory=$env:YS_DIR; $s.WindowStyle=7; "
          "$s.Description='YouSNR transcript server (no window)'; $s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                       capture_output=True, text=True, timeout=60, creationflags=NOWIN)
    if not lnk.exists():
        sys.exit(f"Couldn't create the startup shortcut: {(r.stderr or r.stdout).strip()[:300]}")
    print("YouSNR now turns on by itself, with no window, whenever you sign in to Windows.")
    print(f"  (shortcut: {lnk}; you can also switch it in Task Manager > Startup apps)")
    start_background()


# ---------------------------------------------------------------- can this computer reach YouTube?

def check_youtube(vid="cPmVLrTA8AU", direct=False, browser=True):
    """Try each way YouSNR talks to YouTube and say which work. On a cloud server, this is how you
    find out whether Cloudflare WARP gets past YouTube's block on data-centre addresses."""
    import requests
    if direct:
        os.environ["YOUSNR_PROXY"] = ""
    px = yt_requests_proxies()
    print(f"Checking YouTube access {'through ' + yt_proxy() if yt_proxy() else 'directly (no proxy)'}"
          f"{' over IPv4' if yt_proxy() and yt_ipv4() else ''}, with video {vid}\n")
    passed = []

    def result(n, ok, text):
        passed.append(ok)
        print(f"  {n}. {'OK  ' if ok else 'FAIL'}  {text}")

    try:
        t = requests.get("https://www.cloudflare.com/cdn-cgi/trace", proxies=px, timeout=20).text
        kv = dict(line.split("=", 1) for line in t.splitlines() if "=" in line)
        print(f"  0. YouTube sees the address {kv.get('ip', '?')}  (Cloudflare WARP: {kv.get('warp', 'off')})")
    except Exception as e:
        if yt_proxy():
            print(f"  0. FAIL  couldn't get out through the proxy: {e.__class__.__name__}: {str(e)[:150]}")
            print("\nIs WARP connected?  warp-cli status   (then: warp-cli connect)")
            return False
        print(f"  0. (couldn't look up this computer's address: {e.__class__.__name__})")

    try:                                                   # 1. the watch page (title, music check, chapters)
        r = requests.get(f"https://www.youtube.com/watch?v={vid}&hl=en", proxies=px, timeout=25,
                         headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, cookies={"SOCS": "CAI"})
        pr = _json_after(r.text, "ytInitialPlayerResponse = ") or {}
        ps = pr.get("playabilityStatus") or {}
        title = (pr.get("videoDetails") or {}).get("title", "")
        reason = " ".join(str(ps.get(k) or "") for k in ("reason", "messages")).strip()
        if ps.get("status") == "OK":
            result(1, True, f"watch page: \"{title}\"")
        elif "bot" in reason.lower() or "sign in" in reason.lower():
            result(1, False, "watch page: YouTube says \"Sign in to confirm you're not a bot\"")
        else:
            result(1, False, f"watch page: HTTP {r.status_code}, {ps.get('status') or 'no player data'} {reason[:120]}")
    except Exception as e:
        result(1, False, f"watch page: {e.__class__.__name__}: {str(e)[:150]}")

    try:                                                   # 2. caption files (backup + word-check comparison)
        tracks = list(transcript_api().list(vid))
        result(2, True, f"caption files: {len(tracks)} track(s)")
    except Exception as e:
        result(2, False, f"caption files: {e.__class__.__name__}: {(str(e).strip().splitlines() or [''])[0][:150]}")

    try:                                                   # 3. yt-dlp (audio for word checks)
        import yt_dlp
        class Quiet:
            def debug(self, m): pass
            warning = info = error = debug
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": Quiet(), **yt_dlp_net_opts()}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        audio = [f for f in info.get("formats") or [] if f.get("vcodec") == "none" and f.get("acodec") != "none"]
        result(3, bool(audio), f"audio for word checks: {len(audio)} audio format(s)")
    except Exception as e:
        msg = str(e).replace("ERROR: ", "").strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
        result(3, False, f"audio for word checks: {msg[:170]}")

    if browser:                                            # 4. the real thing: YouTube's transcript panel
        try:
            segs, chapters, track, how = fetch_browser(vid, offscreen=True)
            result(4, bool(segs), f"transcript panel in a browser: {len(segs)} lines ({how})")
        except Exception as e:
            result(4, False, f"transcript panel in a browser: {e.__class__.__name__}: {(str(e).splitlines() or [''])[0][:150]}")

    print()
    if all(passed):
        print("All good: this computer can run YouSNR.")
    elif passed and passed[-1] and browser:
        print("The transcript panel works, so new videos will work. Failed steps above only affect the backups"
              + (" and word checks" if len(passed) > 2 and not passed[2] else "") + ".")
    elif yt_proxy():
        print("YouTube is blocking this computer even through the proxy. On the cloud server, get a new WARP "
              "address and check again:\n  warp-cli disconnect; warp-cli registration delete; "
              "bash ~/yousnr/deploy/oracle-setup.sh")
    else:
        print("YouTube is blocking this computer (or it's offline).")
    return all(passed)


# ---------------------------------------------------------------- CLI

def main():
    load_windows_user_env()
    load_env_file()
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
    up = sub.add_parser("upgrade", help="bring older videos up to the current checking rules")
    up.add_argument("--dry-run", action="store_true", help="only show what would be done and the estimated cost")
    up.add_argument("--limit", type=int, default=0, help="at most this many paid upgrades this run")
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
    sv.add_argument("--log", nargs="?", const=str(LOG), default="", help="write to yousnr.log instead of the screen")
    for name, text in (("start", "turn YouSNR on in the background (no window)"),
                       ("stop", "turn YouSNR off"),
                       ("status", "is it on, is the tunnel up, spending, last lines of the log"),
                       ("background", "the always-on keeper that `start` and the sign-in shortcut run")):
        c = sub.add_parser(name, help=text)
        c.add_argument("--port", type=int, default=8787)
    ck = sub.add_parser("check-youtube", help="can this computer get transcripts from YouTube? (tests the proxy too)")
    ck.add_argument("video", nargs="?", default="cPmVLrTA8AU")
    ck.add_argument("--direct", action="store_true", help="ignore YOUSNR_PROXY (to compare)")
    ck.add_argument("--no-browser", action="store_true", help="skip the slow browser test")
    au = sub.add_parser("autostart", help="on: turn on by itself whenever you sign in to Windows; off: don't")
    au.add_argument("switch", choices=["on", "off"])
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

    if args.cmd == "upgrade":
        if acc is None:
            sys.exit("correct.py is missing")
        index = ensure_index_fields()
        try:
            views = json.loads((DATA / "_views.json").read_text(encoding="utf-8"))
        except Exception:
            views = {}
        free = [v for v, e in index.items() if 0 < e.get("v", 0) < acc.CURRENT and not acc.pending(e)]
        paid = [(v, acc.pending(e)) for v, e in index.items() if acc.pending(e)]
        paid.sort(key=lambda x: -views.get(x[0], 0))
        price = sum(acc.PRICE_PER_HOUR[e] for e in (acc.available() or list(acc.ENGINES)))
        est_secs = 0.0
        for vid, rules in paid:
            e = index[vid]
            if acc.reasons_for(rules) is None:          # never checked: ~1/5 of the video gets sent
                est_secs += 0.2 * (e.get("dur") or 900)
            else:                                       # bleeps: a short clip around each
                est_secs += e.get("bleeps", 0) * (9 + 2 * acc.PAD + acc.GAP)
        print(f"Checking rules: v{acc.CURRENT}. Videos: {len(index)}.")
        for r in acc.RULES:
            n = sum(1 for _, rules in paid if r in rules)
            if n:
                print(f"  v{r['version']} {r['what']}: {n} video(s) need it")
        print(f"  {len(free)} video(s) only need their version updated (free)")
        print(f"  estimated cost for all paid upgrades: ~${est_secs / 3600 * price:.2f} "
              f"({est_secs / 60:.0f} min of audio x {len(acc.available()) or 2} engines); "
              f"this month so far ${acc.spent_this_month():.2f} of ${acc.settings()['budget']:.2f}")
        if args.dry_run:
            sys.exit(0)
        relaid = sum(1 for vid in free if run_upgrade(vid) == "relaid")
        if free:
            print(f"Free updates done ({len(free)} video(s); {relaid} page(s) changed).")
        if not acc.available():
            sys.exit("Set the API keys to run paid upgrades.")
        done = 0
        for vid, rules in paid:
            if args.limit and done >= args.limit:
                break
            print(f"{vid}:")
            try:
                if run_upgrade(vid) not in (None, "stamped", "relaid"):
                    done += 1
            except Exception as e:
                print(f"  stopped: {e.__class__.__name__}: {str(e)[:200]}")
                if e.__class__.__name__ == "OverBudget":
                    break
        write_indexes()
        print(f"Upgraded {done} video(s). Publish with: git add . ; git commit -m \"Upgrade transcripts\" ; git push")

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
        write_atomic(INDEX, json.dumps(index, ensure_ascii=False, indent=0))
        write_indexes(index)

    if args.cmd == "serve":
        if args.log or sys.stdout is None:          # pythonw has no screen to print to
            use_log_file(args.log or LOG)
        serve(args.port, args.lang, push=not args.no_push, tunnel=not args.no_tunnel, browser=not args.captions)
    if args.cmd == "background":
        run_background(args.port)
    if args.cmd == "start":
        start_background(args.port)
    if args.cmd == "stop":
        stop_background()
    if args.cmd == "status":
        show_status(args.port)
    if args.cmd == "autostart":
        set_autostart(args.switch == "on")
    if args.cmd == "check-youtube":
        sys.exit(0 if check_youtube(video_id(args.video), direct=args.direct, browser=not args.no_browser) else 1)


if __name__ == "__main__":
    main()
