"""
YouSNR accuracy layer: fix the words YouTube's transcript most likely got wrong, keeping
YouTube's own lines, timestamps and chapters.

  selective (every new video, cheap)
      1. Flag suspicious lines: where YouTube's two recognizers (transcript panel vs. caption
         file) disagree, plus risky words (unusual acronyms, rare words, names not in the
         title/description).
      2. Cut those lines out of the audio, join them into one short file, and send it to two
         engines (ElevenLabs Scribe v2 and AssemblyAI Universal-3.5 Pro), with words from the
         title, description and chapters as key terms.
      3. Weighted word-by-word vote between YouTube's panel, YouTube's captions and the two
         engines. A word changes only when at least two sources agree on the change and they
         outweigh the sources that agree with YouTube.

  full (videos that become popular)
      The whole audio goes to the primary engine, and its words are poured into YouTube's lines.

Keys come from environment variables, never from files in the project:
  ELEVENLABS_API_KEY, ASSEMBLYAI_API_KEY
Settings (optional environment variables):
  YOUSNR_MONTHLY_BUDGET  (USD, default 5)     YOUSNR_FULL_AT_VIEWS (default 20)
  YOUSNR_PRIMARY         (elevenlabs | assemblyai, default elevenlabs)
"""
import datetime, difflib, html, json, os, re, shutil, subprocess, threading, time, wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = ROOT / "cache"
USAGE = DATA / "_usage.json"

ENGINES = ("elevenlabs", "assemblyai")
PRICE_PER_HOUR = {"elevenlabs": 0.22 * 1.2,       # Scribe v2 + 20% key-terms surcharge
                  "assemblyai": 0.21 + 0.05}      # Universal-3.5 Pro + key-terms add-on
WEIGHT = {"youtube": 1.0, "captions": 0.7, "assemblyai": 1.0, "elevenlabs": 1.1}
PAD = 0.6                  # seconds of extra audio around each flagged stretch
GAP = 1.0                  # seconds of silence between clips in the joined file
MAX_SELECTIVE_SHARE = 0.25  # never send more than this share of a video in selective mode

FILLERS = {"um", "uh", "uhm", "umm", "uhh", "er", "erm", "ah", "hmm", "hm", "mm", "mhm", "huh"}
COMMON_ACRONYMS = set("""I OK TV US USA UK EU AI CEO CFO CTO PM AM FBI CIA NASA NBA NFL NHL MLB UFC FAQ ID IQ
PC PS VR AR DJ MC LA NY NYC DC UN NATO GDP PhD MBA BA BS MD RN ASAP FYI DIY LOL OMG GPS PDF USB WiFi HR IT
COVID SAT GPA BBC CNN ESPN HBO MIT UCLA NYU USC LSU YouTube TikTok iPhone CPU GPU RAM SSD API URL HTML""".split())
NUMBERS = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve "
                                            "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
NUMBERS.update({"thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80",
                "ninety": "90", "hundred": "100", "percent": "%"})
SAME = {"ok": "okay", "cuz": "because", "cause": "because", "alright": "allright", "til": "until", "yea": "yeah"}


# ---------------------------------------------------------------- settings / budget

def settings():
    return {"budget": float(os.environ.get("YOUSNR_MONTHLY_BUDGET", "5")),
            "full_at": int(os.environ.get("YOUSNR_FULL_AT_VIEWS", "20")),
            "primary": os.environ.get("YOUSNR_PRIMARY", "elevenlabs")}


def available():
    """Engines with a key set, primary first."""
    have = [e for e in ENGINES if os.environ.get(f"{e.upper()}_API_KEY")]
    p = settings()["primary"]
    return sorted(have, key=lambda e: e != p)


_usage_lock = threading.Lock()


def _month():
    return datetime.date.today().strftime("%Y-%m")


def _load_usage():
    try:
        return json.loads(USAGE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def spent_this_month():
    return _load_usage().get(_month(), {}).get("usd", 0.0)


def cost(engine, seconds):
    return seconds / 3600 * PRICE_PER_HOUR[engine]


def charge(engine, seconds, vid, mode):
    with _usage_lock:
        u = _load_usage()
        m = u.setdefault(_month(), {"usd": 0.0, "seconds": {}, "videos": 0})
        c = cost(engine, seconds)
        m["usd"] = round(m["usd"] + c, 4)
        m["seconds"][engine] = round(m["seconds"].get(engine, 0) + seconds, 1)
        m.setdefault("log", []).append([datetime.datetime.now().isoformat(timespec="seconds"), vid, mode, engine,
                                        round(seconds, 1), round(c, 4)])
        DATA.mkdir(exist_ok=True)
        tmp = USAGE.with_name(USAGE.name + ".tmp")          # all-or-nothing, so a power cut can't lose the tally
        tmp.write_text(json.dumps(u, indent=1), encoding="utf-8")
        for i in range(20):
            try:
                os.replace(tmp, USAGE)
                break
            except PermissionError:
                time.sleep(0.05 * (i + 1))
        else:
            USAGE.write_text(json.dumps(u, indent=1), encoding="utf-8")
        return c


class OverBudget(Exception):
    pass


# ---------------------------------------------------------------- words

CENSOR = "<censored>"                      # YouTube's bleep: "[ __ ]"


def unbleep_marks(text):
    """Make YouTube's '[ __ ]' one token so it can be lined up against what was actually said."""
    return re.sub(r"\[\s*_+\s*\]", "[__]", text)


_WORD = re.compile(r"\[[^\[\]]{0,60}\]\S*|\([^()]{0,60}\)\S*|\S+")


def words_of(text):
    """Split a line into words, keeping a tag like [sighs and gasps] or (laughs) as one word."""
    return _WORD.findall(unbleep_marks(text))


def norm(tok):
    """Comparison form of a word: lowercase, no punctuation, spelled-out numbers as digits."""
    if tok.startswith("[__]"):
        return CENSOR
    if tok.startswith(("[", "(")) or tok.startswith(">>"):
        return ""
    t = tok.lower().replace("’", "'").replace("‘", "'")
    t = re.sub(r"[^\w'%]+", "", t).strip("'").replace("'", "")
    t = NUMBERS.get(t, t)
    return SAME.get(t, t)


def _core(norms):
    return [n for n in norms if n and n not in FILLERS]


def _strip_punct(s):
    return re.sub(r"^[^\w'$£€]+|[^\w'%]+$", "", s)


def tokens_of(segments):
    """YouTube's lines -> flat word list. Returns (surfaces, norms, line_of_token, line_first_token)."""
    surf, line_of, first = [], [], []
    for i, s in enumerate(segments):
        first.append(len(surf))
        for w in words_of(s["text"]):
            surf.append(w)
            line_of.append(i)
    return surf, [norm(w) for w in surf], line_of, first


def edits_between(p_norm, x_norm, x_surf):
    """Changes that turn YouTube's words (p) into another source's words (x), ignoring filler-only changes.
    Each edit: (i1, i2, core_tuple, surface_list) over p's token indices."""
    out = []
    sm = difflib.SequenceMatcher(None, p_norm, x_norm, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        old, new = _core(p_norm[i1:i2]), _core(x_norm[j1:j2])
        if old == new or (not new and old and all(o == CENSOR for o in old)):
            continue
        while i1 < i2 and p_norm[i1] == "":      # sound tags like [music] at either end stay as they are
            i1 += 1
        while i2 > i1 and p_norm[i2 - 1] == "":
            i2 -= 1
        surf = [s for s, n in zip(x_surf[j1:j2], x_norm[j1:j2]) if n and n not in FILLERS]
        out.append((i1, i2, tuple(new), surf))
    return out


def vote(p_norm, sources, edge_guard=True):
    """sources: {name: (norms, surfaces)}. Weighted vote against YouTube's panel (p).
    Returns accepted edits [(i1, i2, surface_list, [supporters])]."""
    n = len(p_norm)
    proposals = {name: edits_between(p_norm, xn, xs) for name, (xn, xs) in sources.items()}
    if edge_guard:   # padding at clip edges adds/drops a word or two: never act on that alone
        for name in proposals:
            proposals[name] = [e for e in proposals[name]
                               if not ((e[0] == e[1] or not e[2]) and (e[0] <= 1 or e[1] >= n - 1))]
    cands = {}
    for name, eds in proposals.items():
        for i1, i2, core, surf in eds:
            cands.setdefault((i1, i2, core), {"who": [], "surf": {}})
            cands[(i1, i2, core)]["who"].append(name)
            cands[(i1, i2, core)]["surf"][name] = surf

    def overlaps(a1, a2, b1, b2):
        if a1 == a2 or b1 == b2:            # insertions: same gap, or inside the other span
            return a1 == b1 or (b1 < a1 < b2) or (a1 < b1 < a2)
        return a1 < b2 and b1 < a2

    accepted = []
    for (i1, i2, core), c in cands.items():
        w_change = sum(WEIGHT.get(s, 1.0) for s in c["who"])
        bleep = bool(_core(p_norm[i1:i2])) and all(x == CENSOR for x in _core(p_norm[i1:i2]))
        keep = 0.0 if bleep else WEIGHT["youtube"] + sum(
            WEIGHT.get(name, 1.0) for name, eds in proposals.items()
            if name not in c["who"] and not any(overlaps(i1, i2, e[0], e[1]) for e in eds))
        need = 1 if bleep else 2
        if bleep and c["who"] == ["captions"]:
            continue
        if len(c["who"]) >= need and w_change > keep:
            best = max(c["who"], key=lambda s: WEIGHT.get(s, 1.0))
            accepted.append((w_change, i1, i2, c["surf"][best], sorted(c["who"])))
    accepted.sort(key=lambda a: -a[0])
    final = []
    for w, i1, i2, surf, who in accepted:
        if not any(overlaps(i1, i2, f[0], f[1]) for f in final):
            final.append((i1, i2, surf, who))
    return sorted(final)


def adopt(p_norm, x_norm, x_surf):
    """Full mode: take every non-filler difference from the engine."""
    return [(i1, i2, surf, ["engine"]) for i1, i2, core, surf in edits_between(p_norm, x_norm, x_surf)]


def _transplant(p_words, new_words):
    """Put the engine's words in, keeping YouTube's punctuation and capitalisation around them."""
    if not new_words:
        return ""
    if p_words and all(w.startswith("[__]") for w in p_words):          # a bleep: just the word(s)
        return " ".join(_strip_punct(w) or w for w in new_words) + p_words[-1][4:]
    lead = trail = ""
    if p_words:
        lead = "" if p_words[0].startswith("[__]") else re.match(r"^[^\w'$£€]*", p_words[0]).group(0)
        trail = p_words[-1][4:] if p_words[-1].startswith("[__]") else re.search(r"[^\w'%]*$", p_words[-1]).group(0)
    words = [_strip_punct(w) or w for w in new_words]
    words = [w for w in words if w]
    if not words:
        return ""
    if p_words and _strip_punct(p_words[0])[:1].isupper() and words[0][:1].islower():
        words[0] = words[0][0].upper() + words[0][1:]
    return lead + " ".join(words) + trail


def _split_edit(i1, i2, new, surf, line_of):
    """One fix can cover words from two lines, or have a sound tag like [music] inside it. Split it so
    each line gets its share of the new words and tags stay where they were.
    Returns [(line, shown, youtube_text_or_None)] in order."""
    parts = []                                   # ["tag", i] or ["run", [i, ...]]
    for i in range(i1, i2):
        if norm(surf[i]) == "":
            parts.append(["tag", i])
        elif parts and parts[-1][0] == "run" and line_of[parts[-1][1][-1]] == line_of[i]:
            parts[-1][1].append(i)
        else:
            parts.append(["run", [i]])
    runs = [p for p in parts if p[0] == "run"]
    words = [w for w in ([_strip_punct(w) for w in new]) if w]
    out = []
    if not runs:                                 # only tags were there: the new words go in front of them
        if words:
            out.append((line_of[i1], " ".join(words), ""))
        return out + [(line_of[p[1]], surf[p[1]], None) for p in parts]
    total, cum, given = sum(len(r[1]) for r in runs), 0, 0
    share = {}
    for r in runs:                               # new words shared out by how many words each piece replaces
        cum += len(r[1])
        upto = int(len(words) * cum / total + 0.5)
        share[id(r)], given = words[given:upto], upto
    for p in parts:
        if p[0] == "tag":
            out.append((line_of[p[1]], surf[p[1]].replace("[__]", "[ __ ]"), None))
        else:
            old = [surf[i] for i in p[1]]
            out.append((line_of[p[1][0]], _transplant(old, share[id(p)]), " ".join(old).replace("[__]", "[ __ ]")))
    return out


def pieces_by_line(segments, edits, surf, line_of):
    """Apply edits to YouTube's words. Returns {line_index: [[shown_text, youtube_text_or_None], ...]}
    for the lines that changed (youtube_text is "" for a word YouTube left out)."""
    n = len(surf)
    starts = {e[0]: e for e in edits}          # vote() never returns two edits at the same spot
    out = {k: [] for k in range(len(segments))}
    changed = set()
    i = 0
    while i < n or i in starts:
        e = starts.pop(i, None)
        if e:
            i1, i2, new, who = e
            if i2 > i1:
                for line, shown, orig in _split_edit(i1, i2, new, surf, line_of):
                    out[line].append([shown, orig])
                    if orig is not None:
                        changed.add(line)
                i = i2
                continue
            words = [w for w in (_strip_punct(w) for w in new) if w]
            if words:                              # a word YouTube left out
                line = line_of[min(i1, n - 1)]
                out[line].append([" ".join(words), ""])
                changed.add(line)
        if i >= n:
            break
        out[line_of[i]].append([surf[i].replace("[__]", "[ __ ]"), None])
        i += 1
    lines = {}
    for k in sorted(changed):
        merged = []
        for text, orig in out[k]:
            if orig is None and merged and merged[-1][1] is None:
                merged[-1][0] += " " + text
            else:
                merged.append([text, orig])
        lines[str(k)] = merged
    return lines


def relayout(d):
    """Re-apply a video's saved fixes with the current layout rules. Free: no audio is checked again.
    Returns True if what the page shows changes."""
    c = d.get("corrected")
    if not c or not c.get("lines"):
        return False
    segs = d["segments"]
    surf, _, line_of, _ = tokens_of(segs)
    lines = pieces_by_line(segs, edits_from_pieces(segs, c["lines"]), surf, line_of)
    if lines == c["lines"]:
        return False
    c["lines"] = lines
    c["fixes"] = sum(1 for pieces in lines.values() for _, o in pieces if o is not None)
    return True


# ---------------------------------------------------------------- what to check

def context_terms(meta, chapters=()):
    """Key terms for the engines: names, acronyms and numbers from the title, channel, chapters and description."""
    text = " ".join([meta.get("title") or "", meta.get("channel") or ""] + [c.get("title", "") for c in chapters]
                    + [meta.get("description") or ""])
    text = re.sub(r"https?://\S+|www\.\S+|#\w+|@\w+", " ", text)
    terms = []
    terms += re.findall(r"(?<![\w+#])[A-Za-z][A-Za-z0-9]*(?:\+\+|#|\+)(?![\w+#])", text)          # C++, C#, kdb+
    terms += re.findall(r"\b[A-Za-z][\w]*/[A-Za-z][\w]*\b", text)                             # q/kdb
    terms += re.findall(r"\b[A-Z][A-Z0-9&]{1,9}s?\b", text)                                     # ROTC, HRT
    terms += re.findall(r"\b[A-Z][a-z]+(?:[-'][A-Za-z]+)?(?:\s+[A-Z][a-z]+(?:[-'][A-Za-z]+)?){0,2}\b", text)  # Hudson River Trading
    terms += re.findall(r"\b\w*\d\w*\b", text)                                                  # 2024, q4
    terms += re.findall(r"\b[a-z]+[A-Z]\w*\b", text)                                             # iPhone
    terms += [w for w in re.findall(r"\b[a-z]{3,}\b", text) if _zipf(w) < 3.5]                    # quant, leetcode
    out, seen = [], set()
    for t in terms:
        t = re.sub(r"[<>{}\[\]\\]", "", t).strip(" -'")
        k = t.lower()
        if len(t) < 2 or len(t) >= 50 or len(t.split()) > 5 or k in seen or k in FILLERS:
            continue
        if len(t.split()) == 1 and t[:1].isupper() and t[1:].islower() and _zipf(k) > 4.0:
            continue                                     # ordinary capitalised word, e.g. sentence start
        seen.add(k)
        out.append(t)
    return out[:150]


def _zipf(word):
    try:
        from wordfreq import zipf_frequency
        return zipf_frequency(word, "en")
    except Exception:
        return 7.0          # unknown -> treat as common (never flag)


def flag_lines(segments, caption_words, meta, duration):
    """Score each YouTube line by how likely it holds a mistake. Returns [(score, line_index, reasons)]."""
    surf, p_norm, line_of, first = tokens_of(segments)
    ctx = {w.lower() for t in context_terms(meta, meta.get("chapters") or []) for w in t.split()}
    score, why = {}, {}

    def add(i, pts, reason):
        k = line_of[min(max(i, 0), len(line_of) - 1)]
        score[k] = score.get(k, 0) + pts
        why.setdefault(k, set()).add(reason)

    if caption_words:                                   # 1. YouTube's two recognizers disagree
        c_norm = [norm(w) for _, w in caption_words]
        c_surf = [w for _, w in caption_words]
        for i1, i2, core, s in edits_between(p_norm, c_norm, c_surf):
            for i in (range(i1, i2) if i2 > i1 else (i1 - 1, i1)):
                add(i, 2, "YouTube's two transcripts disagree")
    for i, n in enumerate(p_norm):                      # 0. YouTube bleeped a word: find out what it was
        if n == CENSOR:
            add(i, 5, "bleeped by YouTube")
    seen = {}
    for i, w in enumerate(surf):                        # 2. risky words
        core = _strip_punct(w)
        if not core or norm(w) in FILLERS:
            continue
        k = core.lower()
        if k in ctx:
            continue
        reason = None
        if re.fullmatch(r"[A-Z]{2,6}s?", core) and core.rstrip("s") not in COMMON_ACRONYMS:
            reason = "unusual acronym"
        elif core.isalpha() and len(core) > 3 and _zipf(k) < 2.5:
            reason = "rare word"
        elif core[:1].isupper() and i and not surf[i - 1].endswith((".", "?", "!")) and core not in COMMON_ACRONYMS \
                and core != "I" and _zipf(k) < 4.0:
            reason = "name not in the title/description"
        if reason:
            seen[k] = seen.get(k, 0) + 1
            if seen[k] <= 3:                            # don't pay to re-check the same name 30 times
                add(i, 1, reason)
    budget = MAX_SELECTIVE_SHARE * (duration or (segments[-1]["start"] + 10))
    chosen, used = [], 0.0
    for k in sorted(score, key=lambda k: (-score[k], k)):
        ln = _line_len(segments, k, duration)
        if used + ln > budget:
            continue
        chosen.append((score[k], k, sorted(why[k])))
        used += ln
    return sorted(chosen, key=lambda c: c[1])


def _line_len(segments, k, duration):
    end = segments[k + 1]["start"] if k + 1 < len(segments) else (duration or segments[k]["start"] + 8)
    return max(1.0, end - segments[k]["start"])


def windows_for(segments, line_ids, duration):
    """Merge consecutive flagged lines into audio windows: [(start_s, end_s, [line ids])]."""
    wins = []
    for k in sorted(line_ids):
        s = segments[k]["start"]
        e = s + _line_len(segments, k, duration)
        if wins and k == wins[-1][2][-1] + 1:
            wins[-1] = (wins[-1][0], e, wins[-1][2] + [k])
        else:
            wins.append((s, e, [k]))
    return [(max(0.0, s - PAD), e + PAD, ks) for s, e, ks in wins]


# ---------------------------------------------------------------- audio

def ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
    raise RuntimeError("ffmpeg is missing. Run:  python -m pip install --user imageio-ffmpeg")


def download_audio(vid):
    """Audio only, via yt-dlp. (yt-dlp works best with Deno installed:  winget install DenoLand.Deno)"""
    import yt_dlp
    out = CACHE / "audio"
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob(f"{vid}.*"):
        if f.suffix not in (".part", ".ytdl") and f.stat().st_size > 10000:
            return f
    opts = {"format": "bestaudio[ext=m4a]/bestaudio/best", "outtmpl": str(out / f"{vid}.%(ext)s"),
            "quiet": True, "no_warnings": True, "noprogress": True}
    if os.environ.get("YOUSNR_PROXY", "").strip():          # YouTube only: see yt_proxy() in yousnr.py
        opts["proxy"] = os.environ["YOUSNR_PROXY"].strip()
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
            return Path(y.prepare_filename(info))
    except Exception as e:
        raise RuntimeError(f"couldn't download the audio ({str(e)[:150]}). Try: winget install DenoLand.Deno, then "
                           f"python -m pip install --user -U yt-dlp[default]") from None


def to_wav(src, dst):
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)],
                   check=True, capture_output=True, creationflags=0x08000000 if os.name == "nt" else 0)
    return dst


def join_clips(wav_path, windows, out_path):
    """Cut the windows out of a 16 kHz mono WAV and join them with silence. Returns the layout
    [(orig_start, orig_end, offset_in_joined_file)] and the joined length in seconds."""
    with wave.open(str(wav_path), "rb") as w:
        rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
        total = w.getnframes() / rate
        layout, frames, t = [], [], 0.0
        silence = b"\x00" * int(GAP * rate) * width * ch
        for s, e, _ in windows:
            s, e = max(0.0, s), min(total, e)
            if e <= s:
                continue
            w.setpos(int(s * rate))
            frames.append(w.readframes(int((e - s) * rate)))
            layout.append((s, e, t))
            t += (e - s)
            frames.append(silence)
            t += GAP
    with wave.open(str(out_path), "wb") as o:
        o.setnchannels(ch); o.setsampwidth(width); o.setframerate(rate)
        o.writeframes(b"".join(frames))
    return layout, t


def unjoin(words, layout):
    """Engine word times in the joined file -> times in the original video."""
    out = []
    for t, w in words:
        for s, e, off in layout:
            if off - GAP / 2 <= t < off + (e - s) + GAP / 2:
                out.append((s + (t - off), w))
                break
    return out


def audio_seconds(path):
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return None


# ---------------------------------------------------------------- engines

def transcribe(engine, path, terms):
    """-> [(start_seconds, word)] in the file's own timeline. Retries once without key terms if refused."""
    try:
        return _transcribe(engine, path, terms)
    except RuntimeError as e:
        if terms and re.search(r"HTTP 4(00|22)", str(e)):
            return _transcribe(engine, path, [])
        raise


def _transcribe(engine, path, terms):
    import requests
    key = os.environ.get(f"{engine.upper()}_API_KEY")
    if not key:
        raise RuntimeError(f"{engine.upper()}_API_KEY isn't set")
    if engine == "elevenlabs":
        data = [("model_id", "scribe_v2"), ("timestamps_granularity", "word"), ("tag_audio_events", "false")]
        data += [("keyterms", t) for t in terms]
        with open(path, "rb") as fh:
            r = requests.post("https://api.elevenlabs.io/v1/speech-to-text", headers={"xi-api-key": key},
                              data=data, files={"file": (Path(path).name, fh)}, timeout=900)
        if r.status_code != 200:
            raise RuntimeError(f"ElevenLabs HTTP {r.status_code}: {r.text[:200]}")
        return [(float(w["start"]), w["text"]) for w in r.json().get("words", [])
                if w.get("type", "word") == "word" and w.get("text", "").strip()]
    if engine == "assemblyai":
        h = {"authorization": key}
        with open(path, "rb") as fh:
            up = requests.post("https://api.assemblyai.com/v2/upload", headers=h, data=fh, timeout=900)
        if up.status_code != 200:
            raise RuntimeError(f"AssemblyAI upload HTTP {up.status_code}: {up.text[:200]}")
        body = {"audio_url": up.json()["upload_url"], "speech_models": ["universal-3-5-pro", "universal-2"],
                "filter_profanity": False}
        if terms:
            body["keyterms_prompt"] = terms
        r = requests.post("https://api.assemblyai.com/v2/transcript", headers=h, json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"AssemblyAI HTTP {r.status_code}: {r.text[:200]}")
        tid = r.json()["id"]
        for _ in range(600):
            time.sleep(3)
            g = requests.get(f"https://api.assemblyai.com/v2/transcript/{tid}", headers=h, timeout=60).json()
            if g.get("status") == "completed":
                return [(w["start"] / 1000, w["text"]) for w in g.get("words") or [] if w.get("text", "").strip()]
            if g.get("status") == "error":
                raise RuntimeError(f"AssemblyAI: {g.get('error')}")
        raise RuntimeError("AssemblyAI took too long")
    raise ValueError(engine)


def caption_words(vid, lang="en"):
    """YouTube's caption file (its older recognizer) as [(time, word)], times spread across each cue."""
    from youtube_transcript_api import YouTubeTranscriptApi
    px = os.environ.get("YOUSNR_PROXY", "").strip().replace("socks5://", "socks5h://", 1)
    if px:
        from youtube_transcript_api.proxies import GenericProxyConfig
        tl = YouTubeTranscriptApi(proxy_config=GenericProxyConfig(http_url=px, https_url=px)).list(vid)
    else:
        tl = YouTubeTranscriptApi().list(vid)
    try:
        tr = tl.find_generated_transcript([lang, "en"])
    except Exception:
        tr = tl.find_transcript([lang, "en"])
    out = []
    for s in tr.fetch().snippets:
        ws = words_of(html.unescape(s.text))
        for i, w in enumerate(ws):
            out.append((s.start + s.duration * i / max(1, len(ws)), w))
    return out


# ---------------------------------------------------------------- rule versions (upgrades for old videos)
# Every improvement to the checking rules gets a version. Each video remembers the version it was checked
# with. A rule says which videos it affects (cheap test on the index entry) and which kinds of suspicious
# line it needs re-checked, so upgrading an old video only pays for the lines that rule is about.

BLEEP = "bleeped by YouTube"
RULES = [
    {"version": 1, "what": "first check of suspicious lines", "reasons": None,
     "applies": lambda e: True},
    {"version": 2, "what": "restore words YouTube bleeped as [ __ ]", "reasons": {BLEEP},
     "applies": lambda e: e.get("bleeps", 0) > 0},
    {"version": 3, "what": "keep tags like [music] and split fixes that cross two lines (free)", "reasons": set(),
     "applies": lambda e: False},        # nothing to pay for: saved fixes are just laid out again
]
CURRENT = max(r["version"] for r in RULES)


def version_of(d):
    c = d.get("corrected")
    if not c:
        return 0
    if "version" in c:
        return c["version"]
    return 2 if any(BLEEP in (r or []) for _, r in c.get("flagged") or []) else 1   # before versions existed


def bleep_count(d):
    return sum(len(re.findall(r"\[\s*_+\s*\]", s.get("text", ""))) for s in d.get("segments") or [])


def pending(entry):
    """Rules a video (index entry with 'v' and 'bleeps') still needs, oldest first."""
    v = entry.get("v", 0)
    return [r for r in RULES if r["version"] > v and r["applies"](entry)]


def reasons_for(rules):
    """None = check every kind of suspicious line; otherwise just these kinds."""
    if any(r["reasons"] is None for r in rules):
        return None
    out = set()
    for r in rules:
        out |= r["reasons"]
    return out


def _overlaps(a1, a2, b1, b2):
    if a1 == a2 or b1 == b2:
        return a1 == b1 or (b1 < a1 < b2) or (a1 < b1 < a2)
    return a1 < b2 and b1 < a2


def edits_from_pieces(segments, lines):
    """Rebuild earlier fixes (as edits over YouTube's words) from what was saved for the page."""
    surf, p_norm, line_of, first = tokens_of(segments)
    edits, cursor = [], 0
    for k, pieces in sorted((lines or {}).items(), key=lambda kv: int(kv[0])):
        k = int(k)
        if k >= len(first):
            continue
        i = max(first[k], cursor)      # an older fix may have used up the first words of this line
        for text, orig in pieces:
            if orig is None:
                i += len(words_of(text))
            else:
                n_old = len(words_of(orig)) if orig else 0
                edits.append((i, i + n_old, text.split(), ["earlier"]))
                i += n_old
        cursor = i
    return edits


# ---------------------------------------------------------------- the two modes

def correct(d, mode="selective", engines=None, meta=None, log=print, captions=None, budget_check=True,
            only_reasons=None):
    """Work out corrections for one saved video record d. Returns d['corrected'] (also stored in d).
    only_reasons: an upgrade. Re-check only those kinds of suspicious line and keep earlier fixes."""
    vid, segs = d["id"], d["segments"]
    meta = dict(meta or {}, title=(meta or {}).get("title") or d.get("title", ""),
                channel=(meta or {}).get("channel") or d.get("channel", ""))
    meta.setdefault("chapters", d.get("chapters") or [])
    duration = meta.get("duration") or d.get("duration") or (segs[-1]["start"] + 10 if segs else 0)
    engines = [e for e in (engines or available()) if e in ENGINES]
    if not engines:
        raise RuntimeError("no speech-recognition keys set (ELEVENLABS_API_KEY / ASSEMBLYAI_API_KEY)")
    surf, p_norm, line_of, first = tokens_of(segs)
    terms = context_terms(meta, meta["chapters"])
    CACHE.mkdir(exist_ok=True)
    result = {"mode": mode, "engines": [], "at": datetime.datetime.now().isoformat(timespec="seconds"),
              "lines": {}, "fixes": 0, "checked_seconds": 0, "usd": 0.0, "version": CURRENT}
    prev = d.get("corrected") or {}
    old_edits = edits_from_pieces(segs, prev.get("lines")) if only_reasons is not None else []

    if mode == "full":
        est = sum(cost(e, duration) for e in engines)
        if budget_check and spent_this_month() + est > settings()["budget"]:
            raise OverBudget(f"full transcription would cost ~${est:.2f}; monthly budget is ${settings()['budget']:.2f}")
        if captions is None:
            try:
                captions = caption_words(vid)
            except Exception:
                captions = []
        audio = download_audio(vid)
        log(f"    · full transcription with {', '.join(engines)} ({duration / 60:.0f} min of audio, ~${est:.2f})")
        heard = {}
        for eng in engines:
            try:
                ws = [w for _, w in transcribe(eng, audio, terms)]
                heard[eng] = ([norm(w) for w in ws], ws)
                result["usd"] += charge(eng, duration, vid, "full")
                result["engines"].append(eng)
            except Exception as e:
                log(f"    · {eng} failed: {str(e)[:160]}")
        if not heard:
            raise RuntimeError("no engine produced a result")
        if len(heard) >= 2:                      # vote: engine-only slips are outvoted
            sources = dict(heard)
            if captions:
                sources["captions"] = ([norm(w) for _, w in captions], [w for _, w in captions])
            edits = vote(p_norm, sources, edge_guard=False)
        else:                                    # one engine: take its words
            (xn, xs), = heard.values()
            edits = adopt(p_norm, xn, xs)
        result["checked_seconds"] = duration
    else:
        if captions is None and only_reasons is not None and "YouTube's two transcripts disagree" not in only_reasons:
            captions = []                     # this upgrade doesn't need YouTube's caption file
        if captions is None:
            try:
                captions = caption_words(vid)
                log(f"    · YouTube caption file: {len(captions)} words to compare against")
            except Exception as e:
                captions = []
                log(f"    · no caption file to compare against ({e.__class__.__name__}); using risky-word checks only")
        flagged = flag_lines(segs, captions, meta, duration)
        if only_reasons is not None:
            flagged = [f for f in flagged if set(f[2]) & only_reasons]
        if not flagged:
            log("    · nothing to check" if only_reasons is not None else "    · nothing looks suspicious")
            if only_reasons is not None and prev:
                prev.update(version=CURRENT)
                d["corrected"] = prev
                relayout(d)
                return prev
            d["corrected"] = result
            return result
        wins = windows_for(segs, [k for _, k, _ in flagged], duration)
        secs = sum(e - s for s, e, _ in wins) + GAP * len(wins)
        est = sum(cost(e, secs) for e in engines)
        if budget_check and spent_this_month() + est > settings()["budget"]:
            raise OverBudget(f"checking would cost ~${est:.3f}; monthly budget is ${settings()['budget']:.2f}")
        log(f"    · {len(flagged)} suspicious lines in {len(wins)} stretches, {secs:.0f}s of audio, "
            f"{len(engines)} engine(s), ~${est:.3f}")
        audio = download_audio(vid)
        full_wav = to_wav(audio, CACHE / f"{vid}.wav")
        joined = CACHE / f"{vid}.clips.wav"
        layout, joined_len = join_clips(full_wav, wins, joined)
        heard = {}
        for eng in engines:
            try:
                heard[eng] = unjoin(transcribe(eng, joined, terms), layout)
                result["usd"] += charge(eng, joined_len, vid, "selective")
                result["engines"].append(eng)
            except Exception as e:
                log(f"    · {eng} failed: {str(e)[:160]}")
        for f in (full_wav, joined):
            try:
                f.unlink()
            except Exception:
                pass
        if not heard:
            raise RuntimeError("no engine produced a result")
        edits = []
        for s, e, ks in wins:
            a, b = first[ks[0]], (first[ks[-1] + 1] if ks[-1] + 1 < len(segs) else len(surf))
            sources = {}
            for eng, words in heard.items():
                ws = [w for t, w in words if s <= t < e]
                sources[eng] = ([norm(w) for w in ws], ws)
            if captions:
                lo, hi = segs[ks[0]]["start"] - 0.3, (segs[ks[-1] + 1]["start"] if ks[-1] + 1 < len(segs) else e) + 0.3
                cw = [w for t, w in captions if lo <= t < hi]
                sources["captions"] = ([norm(w) for w in cw], cw)
            for i1, i2, new, who in vote(p_norm[a:b], sources):
                edits.append((a + i1, a + i2, new, who))
        result["checked_seconds"] = round(secs, 1)
        result["flagged"] = [[segs[k]["label"] if segs[k].get("label") else k, r] for _, k, r in flagged][:200]
    if only_reasons is not None and prev:     # upgrade: keep earlier fixes that the new ones don't replace
        edits = [e for e in old_edits if not any(_overlaps(e[0], e[1], n[0], n[1]) for n in edits)] + list(edits)
        edits.sort(key=lambda e: (e[0], e[1]))
        result["mode"] = prev.get("mode", result["mode"])
        result["engines"] = sorted(set(prev.get("engines") or []) | set(result["engines"]))
        result["usd"] += prev.get("usd", 0.0)
        result["checked_seconds"] = round(prev.get("checked_seconds", 0) + result["checked_seconds"], 1)
        result["flagged"] = (prev.get("flagged") or []) + result.get("flagged", [])
        result["flagged"] = result["flagged"][:300]
    result["lines"] = pieces_by_line(segs, edits, surf, line_of)
    result["fixes"] = len(edits)
    result["usd"] = round(result["usd"], 4)
    for f in (CACHE / "audio").glob(f"{vid}.*"):     # audio isn't kept
        try:
            f.unlink()
        except Exception:
            pass
    d["corrected"] = result
    return result


# ---------------------------------------------------------------- bake-off

def bakeoff(d, minutes=5, start=None, log=print):
    """Run every engine on the same stretch of the video and show where each disagrees with YouTube."""
    vid, segs = d["id"], d["segments"]
    engines = available()
    if not engines:
        raise RuntimeError("no keys set")
    duration = d.get("duration") or segs[-1]["start"] + 10
    start = start if start is not None else max(0.0, duration / 2 - minutes * 30)
    end = min(duration, start + minutes * 60)
    audio = download_audio(vid)
    wav = to_wav(audio, CACHE / f"{vid}.wav")
    clip = CACHE / f"{vid}.bakeoff.wav"
    layout, length = join_clips(wav, [(start, end, [])], clip)
    terms = context_terms({"title": d.get("title"), "channel": d.get("channel")}, d.get("chapters") or [])
    surf, p_norm, line_of, first = tokens_of(segs)
    idx = [i for i in range(len(surf)) if start <= segs[line_of[i]]["start"] < end]
    a, b = (idx[0], idx[-1] + 1) if idx else (0, 0)
    report = {"from": start, "to": end, "engines": {}}
    for eng in engines:
        words = unjoin(transcribe(eng, clip, terms), layout)
        charge(eng, length, vid, "bakeoff")
        ws = [w for _, w in words]
        eds = edits_between(p_norm[a:b], [norm(w) for w in ws], ws)
        report["engines"][eng] = {"words": len(ws), "differences": len(eds),
                                  "examples": [[segs[line_of[a + min(e[0], b - a - 1)]].get("label"),
                                                " ".join(surf[a + e[0]:a + e[1]]) or "(nothing)",
                                                " ".join(e[3]) or "(nothing)"] for e in eds[:15]]}
    for f in (wav, clip):
        try:
            f.unlink()
        except Exception:
            pass
    return report
