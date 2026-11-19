"""Video frames: what the video looked like at each line or paragraph.

YouTube makes small preview frames of every video ("storyboards": the pictures you see when you drag along
the progress bar), a few seconds apart, packed into sprite sheets on i.ytimg.com. YouSNR saves where they are
(data/frames/ID.json) and the page shows the right one when you click a line; the visitor's browser loads the
pictures straight from YouTube. YouSNR also scores every frame by how much it stands out from the video's usual
shot (a slide, a screenshot, a cutaway), so a click shows the frame that matters most in that stretch, even one
that's only on screen for a second or two. And it picks the moments worth a picture beside the words
(pick_moments): each slide (complete, if it builds up), chart, screenshot or cutaway, never the usual shot.
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
            for k in ("score", "moments", "mode", "v"):   # same frames: keep the scores and pictures
                if k in old:
                    sb[k] = old[k]
        save(d["id"], sb)
    return bool(sb)


def page_json(vid):
    r = result(vid)
    if not r or not r.get("url"):
        return "null"
    keep = {k: r[k] for k in ("url", "w", "h", "cols", "rows", "count", "step", "score", "moments") if k in r}
    return json.dumps(keep, separators=(",", ":")).replace("</", "<\\/")


VERSION = 2                                        # 2: picture moments (pick_moments)

# Picture moments. Frames are compared as small greyscale pictures (32x18 and 64x36, 0-255), by the mean
# difference per pixel.
SCENE_CUT = 14         # 32x18 difference between neighbouring frames that starts a new scene (a cut)
STILL = 1.0            # 64x36: a scene whose neighbours differ less than this is a still (slide, chart, page)...
ACTIVE_PX = 6          # ... not counting pixels that change this much all the time (a webcam in the corner) ...
ACTIVE_SHARE = 0.15    # ... as long as they're under this share of the picture
STEP_CUT = 4.0         # 64x36: inside a still scene, this much change is the next slide
BUILD_TOL = 1.2        # 64x36: a next slide that takes away less than this only adds to the last (a build)
AWAY = 14              # 32x18 difference from the usual shot that makes a frame stand out
FLAT = 8               # pixel spread below this: a blank or black frame
SAME_LIVE = 10         # 32x18: moving shots this close are the same camera set-up
SAME_STILL = 3.0       # 64x36: stills this close are the same slide
RECUR_GAP = 45         # seconds: a picture that matches this far away (outside its own stretch) ...
RECUR_SHARE = 0.12     # ... for this share of the video is a usual shot (a presenter, a second camera angle)
MIN_GAP = 10           # seconds between pictures (twice that between two moving shots)
MAX_PICTURES = 120


def _dist(a, b):
    """Mean difference per pixel between two small greyscale pictures (0-255)."""
    from PIL import ImageChops, ImageStat
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def _spread(a):
    from PIL import ImageStat
    return ImageStat.Stat(a).stddev[0]


def _active(f64, a, b):
    """The pixels that change all the time in frames a..b (a webcam in the corner of slides): a mask
    (255 = changing) and their share of the picture."""
    from PIL import Image, ImageChops
    idx = list(range(a + 1, b + 1))
    idx = idx[::max(1, len(idx) // 40)]
    diffs = [ImageChops.difference(f64[i], f64[i - 1]).getdata() for i in idx]
    on = [255 if sorted(px)[len(px) // 2] > ACTIVE_PX else 0 for px in zip(*diffs)]
    mask = Image.new("L", f64[a].size)
    mask.putdata(on)
    return mask, on.count(255) / len(on)


def _blank(frames, mask):
    from PIL import Image
    flat = Image.new("L", frames[0].size, 128)
    return [Image.composite(flat, f, mask) for f in frames]


def _still(f64, a, b):
    """Is scene a..b a still (slides, a chart, a page being written)? Returns (the frames to compare, mask):
    the mask blanks out a small always-changing corner (or is None), or (None, None) for moving footage."""
    if b <= a:
        return None, None
    inner = sorted(_dist(f64[i], f64[i - 1]) for i in range(a + 1, b + 1))
    if inner[len(inner) // 2] < STILL:
        return f64[a:b + 1], None
    if b - a < 2 or inner[len(inner) // 2] > 6 * STILL:
        return None, None
    mask, share = _active(f64, a, b)
    if share >= ACTIVE_SHARE:
        return None, None
    g = _blank(f64[a:b + 1], mask)
    inner = sorted(_dist(g[i], g[i - 1]) for i in range(1, len(g)))
    return (g, mask) if inner[len(inner) // 2] < STILL else (None, None)


def _builds_on(a, b):
    """Does still b only add to still a (the next bullet, more handwriting)? Ink is darker than a light
    background and lighter than a dark one, so only count change the other way."""
    from PIL import ImageChops, ImageStat
    light = sorted(a.getdata())[a.width * a.height // 2] > 128
    taken = ImageChops.subtract(b, a) if light else ImageChops.subtract(a, b)
    return ImageStat.Stat(taken).mean[0] < BUILD_TOL


def load(sb, get):
    """Every frame as (64x36, 32x18) greyscale pictures, or None where a sheet is short. get(url) -> bytes."""
    from PIL import Image
    per = sb["cols"] * sb["rows"]
    sheets = math.ceil(sb["count"] / per)
    out = []
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
                out.append(None)
                continue
            big = img.crop((c * w, r * h, c * w + w, r * h + h)).resize((64, 36), Image.BOX)
            out.append((big, big.resize((32, 18), Image.BOX)))
    return out


def stand_out(f32):
    """How much each frame stands out from the video's usual shot (the median frame): 0 to 99, and the
    raw differences."""
    from PIL import Image
    good = [v for v in f32 if v]
    if not good:
        return [], []
    usual = Image.new("L", good[0].size)
    usual.putdata([sorted(px)[len(px) // 2] for px in zip(*(g.getdata() for g in good))])
    raw = [_dist(v, usual) if v else 0 for v in f32]
    top = max(raw) or 1
    return [round(99 * x / top) for x in raw], raw


def _share(k, own, frames, step, near):
    """What share of the video (outside this picture's own stretch, RECUR_GAP away) looks like frame k?"""
    stride = max(1, len(frames) // 300)
    hits = sum(1 for m in range(0, len(frames), stride)
               if not own[0] <= m <= own[1] and abs(m - k) * step >= RECUR_GAP and _dist(frames[m], frames[k]) < near)
    return hits * stride / len(frames)


def pick_moments(frames, step):
    """The moments worth a picture beside the transcript: a slide, chart, screenshot or cutaway, not the
    usual shot. Returns ([[start_second, frame], ...], mode). The frame is the last of a slide (so a slide
    that builds up shows complete) and the start is when it first appears."""
    n = len(frames)
    if n < 3:
        return [], "none"
    f64, f32, last = [], [], None
    for f in frames:                                   # a missing frame repeats the one before
        last = f or last
        f64.append(last[0] if last else None)
        f32.append(last[1] if last else None)
    first = next((i for i in range(n) if f32[i]), None)
    if first is None:
        return [], "none"
    for i in range(first):
        f64[i], f32[i] = f64[first], f32[first]
    _, dm = stand_out(f32)
    presenter = sum(x >= AWAY for x in dm) / n < 0.4      # mostly one usual shot (a talking head, a static screen)
    d32 = [999.0] + [_dist(f32[i], f32[i - 1]) for i in range(1, n)]

    pics = []                                          # [first frame, last frame, kind, mask]
    a = 0
    for i in range(1, n + 1):
        if i < n and d32[i] < SCENE_CUT:
            continue
        b = i - 1
        g, mask = _still(f64, a, b)
        if g:                                          # a still scene: one picture per slide, builds joined
            p0 = a
            for j in range(a + 1, b + 2):
                if j <= b and _dist(g[j - a], g[j - 1 - a]) < STEP_CUT:
                    continue
                if pics and pics[-1][2] == "still" and pics[-1][1] == p0 - 1 and p0 > a and (
                        _builds_on(g[pics[-1][1] - a], g[j - 1 - a]) or _dist(g[pics[-1][1] - a], g[j - 1 - a]) < SAME_STILL):
                    pics[-1][1] = j - 1
                else:
                    pics.append([p0, j - 1, "still", mask])
                p0 = j
        else:
            pics.append([a, b, "live", None])
        a = i

    cands, blanked = [], {}
    for p0, p1, kind, mask in pics:
        dur = (p1 - p0 + 1) * step
        if kind == "still":
            rep = next((k for k in range(p1, p0 - 1, -1) if _spread(f32[k]) >= FLAT), None)
            if rep is None or dur > 0.4 * n * step:
                continue                               # blank, or on screen for most of the video
            if mask is not None and id(mask) not in blanked:   # compare with the same corner blanked out everywhere
                blanked[id(mask)] = _blank(f64, mask)
            cmp = blanked[id(mask)] if mask is not None else f64
            look = sorted({p0 + (p1 - p0) * q // 4 for q in range(5)} | {rep})
            if 2 * sum(_share(k, (p0, p1), cmp, step, SAME_STILL) >= RECUR_SHARE for k in look) >= len(look):
                continue                               # most of it is all over the video: a presenter who barely moves
        else:
            mid = (p0 + p1) // 2
            rep = mid if _spread(f32[mid]) >= FLAT else next((k for k in range(p0, p1 + 1) if _spread(f32[k]) >= FLAT), None)
            if rep is None or (presenter and max(dm[p0:p1 + 1]) < AWAY):
                continue                               # black, or the usual shot
            if _share(rep, (p0, p1), f32, step, SAME_LIVE) >= RECUR_SHARE:
                continue                               # a camera set-up that's all over the video
        value = (3 + min(dur, 60) / 60) if kind == "still" else (1 + min(dur, 30) / 30 + dm[rep] / 100)
        cands.append({"t": p0 * step, "rep": rep, "kind": kind, "value": value})

    firsts = []                                        # the same slide or shot again: keep where it first appears
    for c in cands:
        if not any(_dist(f64[o["rep"]], f64[c["rep"]]) < SAME_STILL or
                   (o["kind"] == c["kind"] == "live" and _dist(f32[o["rep"]], f32[c["rep"]]) < SAME_LIVE) for o in firsts):
            firsts.append(c)
    cands = firsts

    dur = n * step
    room = {"still": max(2, round(dur / 20)), "live": max(1, round(dur / (20 if presenter else 60)))}
    cands = sorted(cands, key=lambda c: -c["value"])[:MAX_PICTURES * 3]
    out = []
    for c in sorted(cands, key=lambda c: -c["value"]):
        if room[c["kind"]] <= 0 or any(abs(o["t"] - c["t"]) < (2 * MIN_GAP if o["kind"] == c["kind"] == "live" else MIN_GAP) for o in out):
            continue
        room[c["kind"]] -= 1
        out.append(c)
        if len(out) >= MAX_PICTURES:
            break
    out.sort(key=lambda c: c["t"])
    return [[round(c["t"], 1), c["rep"]] for c in out], "presenter" if presenter else "visual"


def score(sb, get):
    """Score the frames (sb["score"]) and pick the picture moments (sb["moments"]). Needs Pillow."""
    frames = load(sb, get)
    sb["score"], _ = stand_out([f[1] if f else None for f in frames])
    sb["moments"], sb["mode"] = pick_moments(frames, sb["step"])
    sb["v"] = VERSION
    return sb


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
            if r and ((r.get("score") and r.get("v", 1) >= VERSION) or r.get("unscorable")):
                continue
            if time.time() - self.tried.get(vid, 0) < 6 * 3600:
                continue
            out.append((f.stat().st_mtime, vid))
        return [v for _, v in sorted(out, reverse=True)]

    def fresh(self, vid, ask=False):
        """Where the frames are: from the spec saved at fetch time, or (ask=True, or none saved) yt-dlp."""
        d = json.loads((DATA / f"{vid}.json").read_text(encoding="utf-8"))
        r = None if ask else from_spec(d.get("storyboard_spec"), d.get("duration") or 0)
        return r or from_ytdlp(self.ytdlp_info(vid) or {})

    def one(self, vid, log=print):
        r = result(vid) or self.fresh(vid)
        if not r:
            raise RuntimeError("YouTube has no preview frames for this video")
        try:
            try:
                score(r, self.get)
            except ImportError:
                raise
            except Exception:
                if not r.get("score"):
                    raise
                r = self.fresh(vid, ask=True)          # scored before: the saved address may be stale
                if not r:
                    raise
                score(r, self.get)
        except ImportError:
            r["unscorable"] = "Pillow isn't installed"
        save(vid, r)
        log(f"  frames for {vid}: {r['count']} frames, {r['w']}x{r['h']}, one every {r['step']:.1f}s"
            + (f", {len(r.get('moments') or [])} pictures ({r.get('mode')})" if r.get("score") else ""))
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
