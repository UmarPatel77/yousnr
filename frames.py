"""Video frames: what the video looked like at each line or paragraph.

YouTube makes small preview frames of every video ("storyboards": the pictures you see when you drag along
the progress bar), a few seconds apart, packed into sprite sheets on i.ytimg.com. YouSNR saves where they are
(data/frames/ID.json) and the page shows the right one when you click a line; the visitor's browser loads the
pictures straight from YouTube. YouSNR also scores every frame by how much it stands out from the video's usual
shot (a slide, a screenshot, a cutaway), so a click shows the frame that matters most in that stretch, even one
that's only on screen for a second or two.
"""
import io
import json
import math
import os
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FR_DIR = DATA / "frames"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def result(vid):
    try:
        return json.loads((FR_DIR / f"{vid}.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def from_spec(spec, duration):
    """YouTube's storyboard spec ("base|w#h#count#cols#rows#interval#name#sigh|...", the same format yt-dlp
    reads) -> the largest frames: {url (with $M for the sheet number), w, h, cols, rows, count, step}."""
    parts = (spec or "").split("|")
    base = parts[0] if parts and parts[0].startswith("http") else ""
    if not base or len(parts) < 2 or not duration:
        return None
    best = None
    for level, args in enumerate(parts[1:]):
        a = args.split("#")
        if len(a) != 8:
            continue
        try:
            w, h, count, cols, rows = (int(x) for x in a[:5])
        except ValueError:
            continue
        if not (w and h and count and cols and rows):
            continue
        if best is None or w > best["w"]:
            url = base.replace("$L", str(level)).replace("$N", a[6]) + f"&sigh={a[7]}"
            best = {"url": url, "w": w, "h": h, "cols": cols, "rows": rows, "count": count,
                    "step": round(float(duration) / count, 3)}
    return best


def from_ytdlp(info):
    """The same, from yt-dlp's storyboard formats (for videos fetched before frames were saved)."""
    dur = info.get("duration") or 0
    best = None
    for f in info.get("formats") or []:
        if f.get("format_note") != "storyboard" or not f.get("url") or "$M" not in f["url"] or not f.get("fps"):
            continue
        if best is None or (f.get("width") or 0) > best["w"]:
            count = int(round(f["fps"] * dur)) if dur else 0
            if not count:
                continue
            best = {"url": f["url"], "w": int(f["width"]), "h": int(f["height"]), "cols": int(f["columns"]),
                    "rows": int(f["rows"]), "count": count, "step": round(1 / f["fps"], 3)}
    return best


def save(vid, sb):
    _write(FR_DIR / f"{vid}.json", json.dumps(sb, separators=(",", ":")))


def from_video(d):
    """At fetch time: save the frames from the spec the browser saw. Returns True if saved."""
    sb = from_spec(d.get("storyboard_spec"), d.get("duration") or 0)
    if sb:
        old = result(d["id"]) or {}
        if old.get("url", "").split("?")[0] == sb["url"].split("?")[0] and old.get("score"):
            sb["score"] = old["score"]                 # same frames: keep the scores
        save(d["id"], sb)
    return bool(sb)


def page_json(vid):
    r = result(vid)
    if not r or not r.get("url"):
        return "null"
    keep = {k: r[k] for k in ("url", "w", "h", "cols", "rows", "count", "step", "score") if k in r}
    return json.dumps(keep, separators=(",", ":")).replace("</", "<\\/")


def score(sb, get):
    """How much each frame stands out from the video's usual shot: 0 (the usual) to 99 (the most different).
    get(url) -> bytes. Needs Pillow."""
    from PIL import Image
    per = sb["cols"] * sb["rows"]
    sheets = math.ceil(sb["count"] / per)
    vecs = []
    for s in range(sheets):
        img = Image.open(io.BytesIO(get(sb["url"].replace("$M", str(s))))).convert("L")
        w, h = sb["w"], sb["h"]
        if img.width < sb["cols"] * w:                 # trust the picture over the spec
            w, h = img.width // sb["cols"], img.height // max(1, math.ceil(img.height / h))
        for k in range(per):
            i = s * per + k
            if i >= sb["count"]:
                break
            c, r = k % sb["cols"], k // sb["cols"]
            if (r + 1) * h > img.height:
                vecs.append(None)
                continue
            vecs.append(list(img.crop((c * w, r * h, c * w + w, r * h + h)).resize((16, 9)).getdata()))
    good = [v for v in vecs if v]
    if not good:
        return []
    usual = [sorted(px)[len(px) // 2] for px in zip(*good)]          # the median frame: the usual shot
    raw = [sum(abs(a - b) for a, b in zip(v, usual)) / len(usual) if v else 0 for v in vecs]
    top = max(raw) or 1
    return [round(99 * x / top) for x in raw]


class Framer:
    """Background: saves frames for videos that don't have them yet (from the spec saved at fetch time, or
    through yt-dlp for older videos) and scores them. New videos first, at most `per_hour` an hour."""

    def __init__(self, on_done, ytdlp_info, get, per_hour=30):
        self.on_done, self.ytdlp_info, self.get, self.per_hour = on_done, ytdlp_info, get, per_hour
        self.tried = {}                                # video -> when it last failed
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def todo(self):
        out = []
        for f in DATA.glob("*.json"):
            vid = f.stem
            if not ID_RE.match(vid):
                continue
            r = result(vid)
            if r and (r.get("score") or r.get("unscorable")):
                continue
            if time.time() - self.tried.get(vid, 0) < 6 * 3600:
                continue
            out.append((f.stat().st_mtime, vid))
        return [v for _, v in sorted(out, reverse=True)]

    def one(self, vid, log=print):
        r = result(vid)
        if not r:
            d = json.loads((DATA / f"{vid}.json").read_text(encoding="utf-8"))
            r = from_spec(d.get("storyboard_spec"), d.get("duration") or 0)
            if not r:
                info = self.ytdlp_info(vid)
                r = from_ytdlp(info or {})
            if not r:
                raise RuntimeError("YouTube has no preview frames for this video")
        try:
            r["score"] = score(r, self.get)
        except ImportError:
            r["unscorable"] = "Pillow isn't installed"
        save(vid, r)
        log(f"  frames for {vid}: {r['count']} frames, {r['w']}x{r['h']}, one every {r['step']:.1f}s"
            + (", scored" if r.get("score") else ""))
        return r

    def _loop(self):
        self.stop.wait(90)
        while not self.stop.is_set():
            todo, vid = self.todo(), None
            try:
                if todo:
                    vid = todo[0]
                    self.one(vid)
                    self.on_done(vid)
            except Exception as e:
                self.tried[vid] = time.time()
                print(f"  frames for {vid} failed: {e.__class__.__name__}: {str(e)[:200]}")
            self.stop.wait(3600 / self.per_hour if vid else 600)
