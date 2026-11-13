"""In their words: the few fragments of a transcript that *are* the video.

Claude picks them (taste, not a summary), but only says where they are: code copies the words out of the
transcript, so a fragment that isn't word for word in it is dropped and every word shown was really said.
Results live in data/excerpt/ID.json and show at the top of the page.

Settings (environment, or ~/.config/yousnr.env on a server):
  ANTHROPIC_API_KEY            turns it on
  YOUSNR_EXCERPT_MODEL         default claude-sonnet-5-5
  YOUSNR_EXCERPT_BUDGET        dollars a month, default 3
  YOUSNR_EXCERPTS_PER_HOUR     how many the server makes an hour at most (new videos first), default 20
"""
import datetime
import json
import os
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
EX_DIR = DATA / "excerpt"
USAGE = DATA / "_excerpt_usage.json"            # this month's spending (kept on this computer only)
API = "https://api.anthropic.com/v1/messages"
PRICES = {                                       # USD per million tokens (input, output), Nov 2026
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
MAX_FRAGMENTS, MAX_WORDS, MAX_FRAGMENT_WORDS = 14, 70, 12
VERSION = 3                                      # bump when the instructions change: older ones get remade
FRESH = 10 * 60                                  # wait this long after a video changes (its word check runs first)
RETRY_FAILED = 3 * 86400
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

SYSTEM = """You cut YouTube transcripts down to the few fragments that are the video.

Not a summary. Not what the video is about. What it *was*: the words the people in it would point to and say "yeah, that's it, that's my thing." This is taste, not judgment. Excise at the frontier: keep only what carries the feel, and trust pattern recognition over importance. Be a little flippant about it.

Keep fragments short. Lift a term, a name, a phrase, half a sentence; almost never a whole sentence, even when the material is dense or technical. "term … tech … phrase" beats one long quote. A fragment is usually 2 to 8 words and never more than 12.

Every fragment should lead somewhere if someone typed those exact words into a search engine: a term of art, a named thing (person, tool, place, event, game, law, paper, case), a coined phrase, a distinctive claim. The gaps between fragments are fine; a curious reader fills them by searching the fragments and lands on the blogs, Stack Exchange answers, Reddit threads, posts, textbooks and papers about the same idea. Plain words that would lead nowhere ("the thing is", "really important") don't earn a place unless they're the speaker's signature.

Lift each term where the speaker shows their angle on it: what they use it for, what they think of it, why it matters to them, the case they make, the verdict. Their use case, not the textbook definition or how to implement it.

Keep:
- the memorable phrasing, the turn of phrase, the line that lands
- jokes, bits, quirks, verbal tics, odd word choices, even mistakes, when they're part of how the point got made
- the take, the peak of the rant, the confession, the punchline, the thing said with conviction
- if the people are just having fun, the fun. If it's bleak or negative, keep it bleak or negative. Don't sanitize, balance or moralize.

This works the same for any kind of video: programming and engineering (the tools, techniques and the maker's take on them), science (the phenomenon and the claim), humanities and history (names, works, coined concepts, the thesis), politics (the policy, the people, the stance), video game essays (the game, the mechanic, the verdict), true crime (names, places, the detail that turns the case), comedy and hangouts (the bits).

Skip intros and outros, "like and subscribe", sponsor reads, housekeeping, and filler any video could have. Prefer what only this video would contain.

Rules:
- Copy every fragment word for word from the transcript: the same words, in the same order, next to each other, nothing added, nothing fixed. A fragment may run from the end of one line into the next.
- Give the line number where each fragment starts.
- 1 to 14 fragments, in the order they occur, no more than 70 words in total. Fewer is better when fewer says it.
- It doesn't have to be coherent or explain anything. Read in order with "\u2026" between them, it should feel conclusive, like the video's own last word on itself.
- Keep the transcript's language. Don't translate."""

SCHEMA = {                                       # the answer's shape (structured outputs; counts are checked in build())
    "type": "object",
    "properties": {
        "fragments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer", "description": "line number where the fragment starts"},
                    "quote": {"type": "string", "description": "the exact words, copied from the transcript"},
                },
                "required": ["line", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["fragments"],
    "additionalProperties": False,
}


def settings():
    e = os.environ.get
    model = e("YOUSNR_EXCERPT_MODEL") or "claude-sonnet-5-5"
    return {"key": (e("ANTHROPIC_API_KEY") or "").strip(), "model": model,
            "budget": float(e("YOUSNR_EXCERPT_BUDGET") or 3),
            "per_hour": max(1, int(e("YOUSNR_EXCERPTS_PER_HOUR") or 20)),
            "price": PRICES.get(model, PRICES["claude-opus-5-5"])}   # unknown model: assume the dearest


def spent():
    try:
        u = json.loads(USAGE.read_text(encoding="utf-8"))
        return float(u.get("spent", 0)) if u.get("month") == time.strftime("%Y-%m") else 0.0
    except Exception:
        return 0.0


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _record(cost):
    _write(USAGE, json.dumps({"month": time.strftime("%Y-%m"), "spent": round(spent() + cost, 5)}))


def result(vid):
    try:
        return json.loads((EX_DIR / f"{vid}.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def lines_of(d):
    """[(segment index, start, end, text)] as the page shows it: corrections in, [tags] out."""
    fixed = (d.get("corrected") or {}).get("lines") or {}
    segs, out = d["segments"], []
    for i, s in enumerate(segs):
        pieces = fixed.get(str(i))
        t = " ".join(p for p, _ in pieces if p) if pieces else s["text"]
        t = re.sub(r"[\s\u200b-\u200d\ufeff]+", " ", re.sub(r"\[[^\[\]]{0,60}\]", " ", t)).strip()
        if not t:
            continue
        start = float(s["start"])
        nxt = float(segs[i + 1]["start"]) if i + 1 < len(segs) else None
        dur = float(s.get("dur") or 0)
        end = min(nxt, start + dur) if (nxt and dur) else (nxt or start + (dur or 4))
        out.append((i, start, max(end, start), t))
    return out


def _norm(w):
    w = w.replace("’", "'").replace("‘", "'").lower()
    return re.sub(r"^[^\w']+|[^\w']+$", "", w)


def locate(quote, lines, near):
    """Find quote word for word (ignoring case and punctuation at word edges) in the transcript, nearest to
    line `near`. Returns (first word, last word) as (line, word) positions, or None."""
    want = [w for w in (_norm(x) for x in quote.split()) if w]
    if not want:
        return None
    flat = [(li, wi, _norm(w)) for li, (_, _, _, t) in enumerate(lines) for wi, w in enumerate(t.split())]
    flat = [f for f in flat if f[2]]
    best = None
    for k in range(len(flat) - len(want) + 1):
        if flat[k][2] == want[0] and all(flat[k + j][2] == want[j] for j in range(1, len(want))):
            dist = abs(flat[k][0] - near)
            if best is None or dist < best[0]:
                best = (dist, k)
    if best is None:
        return None
    k = best[1]
    return (flat[k][0], flat[k][1]), (flat[k + len(want) - 1][0], flat[k + len(want) - 1][1])


def _text_between(lines, a, b):
    words = []
    for li in range(a[0], b[0] + 1):
        ws = lines[li][3].split()
        words += ws[(a[1] if li == a[0] else 0):(b[1] + 1 if li == b[0] else len(ws))]
    text = " ".join(words)
    return re.sub(r"[.,;:–—-]+$", "", text).strip()   # the "…" after it does that job (? and ! stay)


def _time_of(lines, pos):
    _, start, end, t = lines[pos[0]]
    ws = t.split()
    before = sum(len(w) + 1 for w in ws[:pos[1]])
    return start + (end - start) * before / max(1, len(t) + 1)


def build(lines, picks):
    """Turn the model's picks into verified fragments: verbatim only, no overlaps, in order, capped."""
    found = []
    for p in picks:
        try:
            near = int(p.get("line", 1)) - 1
        except Exception:
            near = 0
        loc = locate(str(p.get("quote") or ""), lines, near)
        if loc:
            found.append(loc)
    found.sort()
    out, last_end, words = [], None, 0
    for a, b in found:
        if last_end is not None and a <= last_end:     # overlaps the one before: skip it
            continue
        text = _text_between(lines, a, b)
        n = len(text.split())
        if not n or n > MAX_FRAGMENT_WORDS or words + n > MAX_WORDS or len(out) >= MAX_FRAGMENTS:
            continue
        out.append({"text": text, "t": round(_time_of(lines, a), 1), "line": lines[a[0]][0]})
        last_end, words = b, words + n
    return out


def ask(d, lines, st):
    """One Claude call. Returns (picks, cost)."""
    import requests
    numbered = "\n".join(f"{n}|{t}" for n, (_, _, _, t) in enumerate(lines, 1))
    user = (f"Title: {d.get('title') or ''}\nChannel: {d.get('channel') or ''}\n\n"
            f"Transcript (line number | words):\n{numbered}")
    body = {"model": st["model"], "max_tokens": 4000, "system": SYSTEM,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}}
    headers = {"x-api-key": st["key"], "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for attempt in range(3):
        r = requests.post(API, headers=headers, json=body, timeout=180)
        if r.status_code in (429, 500, 502, 503, 529) and attempt < 2:
            time.sleep(20 * (attempt + 1))
            continue
        break
    if r.status_code >= 400:
        raise RuntimeError(f"Claude API HTTP {r.status_code}: {r.text[:300]}")
    res = r.json()
    u = res.get("usage") or {}
    pin, pout = st["price"]
    cost = (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout) / 1e6
    if res.get("stop_reason") in ("refusal", "max_tokens"):
        raise AnswerProblem(f"Claude stopped early ({res.get('stop_reason')})", cost)
    text = "".join(c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text")
    try:
        picks = json.loads(text).get("fragments") or []
    except Exception:
        raise AnswerProblem(f"Claude's answer wasn't the expected JSON: {text[:120]!r}", cost)
    return picks, cost


class AnswerProblem(RuntimeError):
    """The call worked (and cost something) but the answer can't be used."""
    def __init__(self, msg, cost):
        super().__init__(msg)
        self.cost = cost


def estimate(lines, st):
    chars = len(SYSTEM) + sum(len(t) + 6 for _, _, _, t in lines) + 200
    pin, pout = st["price"]
    return (chars / 3.5 * pin + 1500 * pout) / 1e6


def make(vid, quiet=False):
    """Make (or remake) one video's excerpt. Returns the saved result, or raises."""
    st = settings()
    if not st["key"]:
        raise RuntimeError("no ANTHROPIC_API_KEY")
    d = json.loads((DATA / f"{vid}.json").read_text(encoding="utf-8"))
    lines = lines_of(d)
    if sum(len(t.split()) for *_, t in lines) < 8:
        raise RuntimeError("too short")
    if spent() + estimate(lines, st) > st["budget"] + 1e-9:
        raise BudgetUsed(f"this month's ${st['budget']:.2f} is used up")
    try:
        picks, cost = ask(d, lines, st)
    except AnswerProblem as e:
        _record(e.cost)
        _write(EX_DIR / f"{vid}.json", json.dumps({"made": _now(), "v": VERSION, "fragments": [], "failed": str(e)}))
        raise
    _record(cost)
    frags = build(lines, picks)
    out = {"made": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "v": VERSION,
           "model": st["model"], "fragments": frags, "cost": round(cost, 5),
           "dropped": len(picks) - len(frags)}
    if not frags:
        out["failed"] = "no fragment matched the transcript word for word"
    _write(EX_DIR / f"{vid}.json", json.dumps(out, ensure_ascii=False, indent=1))
    if not quiet:
        print(f"  {vid}: " + (" … ".join(f["text"] for f in frags) or out["failed"])
              + f"   (${cost:.3f}; ${spent():.2f} of ${st['budget']:.2f} this month)")
    return out


class BudgetUsed(RuntimeError):
    pass


def html(vid, esc):
    """The block at the top of the page, or "" when there isn't one."""
    r = result(vid)
    frags = (r or {}).get("fragments") or []
    if not frags:
        return ""
    gap = ' <span class="gap">…</span> '
    body = gap.join(f'<a href="#t={int(f["t"])}" data-t="{int(f["t"])}">{esc(f["text"])}</a>' for f in frags)
    return ('<figure class="cut" id="cut"><figcaption title="Picked from the transcript by AI. Every word here was said '
            'in the video; tap one to jump there.">In their words</figcaption>'
            f'<blockquote>{body}</blockquote></figure>')


def plain(vid):
    frags = ((result(vid) or {}).get("fragments")) or []
    return " … ".join(f["text"] for f in frags)


class Excerpter:
    """Makes excerpts in the background: new videos about 10 minutes after they're added (after their
    word check), then older ones, most-viewed first, at most YOUSNR_EXCERPTS_PER_HOUR an hour."""

    def __init__(self, on_done, views=None):
        self.on_done, self.views = on_done, views or (lambda: {})
        self.stop = threading.Event()
        self.warned = set()

    def enabled(self):
        return bool(settings()["key"])

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def todo(self):
        now, views = time.time(), self.views()
        new, old, redo = [], [], []
        for f in DATA.glob("*.json"):
            vid = f.stem
            if not ID_RE.match(vid):
                continue
            age = now - f.stat().st_mtime
            if age < FRESH:
                continue
            r = result(vid)
            if r and not r.get("failed") and r.get("v", 1) < VERSION:
                redo.append((views.get(vid, 0), f.stat().st_mtime, vid))      # made under older instructions
                continue
            if r and r.get("failed") and (str(r["failed"]).startswith("Claude API") or r.get("v", 1) < VERSION):
                r = None                                   # not this video's fault: try it again now
            if r and not (r.get("failed") and now - _when(r) > RETRY_FAILED):
                continue
            (new if age < 2 * 86400 else old).append((f.stat().st_mtime, views.get(vid, 0), vid))
        new.sort(reverse=True)
        old.sort(key=lambda x: (-x[1], -x[0]))
        redo.sort(reverse=True)
        return [v for *_, v in new + old + redo]

    def _loop(self):
        self.stop.wait(60)                       # let the server settle first
        while not self.stop.is_set():
            st = settings()
            gap = 3600 / st["per_hour"]
            vid = None
            try:
                if st["key"]:
                    todo = self.todo()
                    vid = todo[0] if todo else None
                if vid:
                    make(vid)
                    self.on_done(vid)
            except BudgetUsed as e:
                if "budget" not in self.warned:
                    print(f"  excerpts paused: {e}")
                    self.warned.add("budget")
                self.stop.wait(3600)
                continue
            except Exception as e:
                msg = str(e)
                print(f"  excerpt for {vid} failed: {e.__class__.__name__}: {msg[:300]}")
                if vid and ("prompt is too long" in msg or "HTTP 413" in msg):     # this video is too long
                    _write(EX_DIR / f"{vid}.json", json.dumps({"made": _now(), "v": VERSION, "fragments": [], "failed": msg[:300]}))
                elif "Claude API HTTP 4" in msg and "HTTP 429" not in msg:
                    # the key, credits, model or request: the same for every video, so wait instead of
                    # marking them all failed
                    print("  excerpts: pausing an hour (restart YouSNR after fixing the problem above)")
                    self.stop.wait(3600)
                    continue
            self.stop.wait(gap if vid else 300)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _when(r):
    try:
        return datetime.datetime.fromisoformat(r.get("made")).timestamp()
    except Exception:
        return 0
