# YouSNR

*All signal, no noise.* YouTube → You**SNR**, as in signal-to-noise ratio.

YouTube-style transcript pages at `yousnr.com/<videoID>`, hosted free on GitHub Pages.
No video player, just the transcript, with chapter headings, timestamps, search, copy and .txt/.srt download.

The trick: swap `tube` for `snr` in any YouTube link:

```
youtube.com/watch?v=265-EZJ_aJE   →   yousnr.com/watch?v=265-EZJ_aJE   →   yousnr.com/265-EZJ_aJE/
```

These URL shapes all work: `/ID`, `/watch?v=ID`, `/shorts/ID`, `/embed/ID`, `/live/ID`, `/youtu.be/ID`, and a pasted full URL. A `&t=90s` jumps to that line.

## Start here (Windows)

1. **Install** (once): double-click `install.bat`.
2. **Connect to GitHub** (once). Click this folder's address bar in File Explorer, type `cmd`, press Enter, then run:
   ```
   git init -b main
   git remote add origin https://github.com/UmarPatel77/yousnr.git
   git add -A
   git commit -m "fresh start"
   git push -u origin main --force
   ```
   `--force` replaces what's on GitHub with this folder. GitHub Pages settings stay as they are.
3. **Run the server**: double-click `start-yousnr.bat`, then open http://localhost:8787/watch?v=cPmVLrTA8AU
4. **Go public**: set up the Cloudflare Tunnel (below), then anyone can use `yousnr.com/watch?v=ANY_ID`.

Commands:

| Command | What it does |
|---|---|
| `python yousnr.py add <video>` | add a video (`--show` to watch the browser if the backup kicks in) |
| `python yousnr.py remove <video>` | delete a video from the site |
| `python yousnr.py serve` | on-demand server + tunnel (what `start-yousnr.bat` runs); close to turn off |
| `python yousnr.py build` | rebuild all pages after editing `templates/` |
| `python yousnr.py paste <video> [file]` | build from a transcript copied off YouTube |
| `python yousnr.py raw <video>` | save raw caption files to `debug/raw/` for troubleshooting |

## How it works

GitHub Pages only serves static files and can't fetch from YouTube. So transcripts are fetched on **your computer**, saved to `data/<ID>.json`, and turned into plain HTML pages in `docs/`. You then push.

- `docs/<ID>/index.html` is one real page per video (fast, and Google can index it).
- `docs/404.html` doubles as the router. GitHub Pages serves it for any path that doesn't exist, and it reads the URL and redirects to `/<ID>/`. If that ID hasn't been added, it shows "No transcript for this video yet".

## Adding videos

```bash
pip install youtube-transcript-api yt-dlp playwright

python yousnr.py add https://www.youtube.com/watch?v=265-EZJ_aJE   # one or many; links or IDs (or let `serve` do it)
git add -A && git commit -m "add transcript" && git push
```

`add` gets the transcript exactly as YouTube's *Show transcript* panel shows it: same lines, timestamps and chapter headings, from the same track YouTube picks by default. YouTube only gives that out to a real browser, so `add` opens the video in a hidden Chrome or Edge window (needs `python -m pip install --user playwright`), presses *Show transcript*, and reads the result. If that fails, it falls back to the raw caption file regrouped to roughly match the panel, and says so. Flags: `--show` shows the browser window, `--debug` saves YouTube's replies and a screenshot to `debug/`, `--lang es` picks another language.

**Run `add` from home internet, not a server.** YouTube blocks most cloud and datacenter IPs, and that includes GitHub Actions runners, so automating it in CI won't work without residential proxies. If you get `RequestBlocked`/`IpBlocked` even at home, you've made too many requests: wait, or use the paste fallback.

**Copy-paste (always exact).** On YouTube, open the transcript (*...more → Show transcript*), select everything in the panel, copy it, then run:

```bash
python yousnr.py paste 265-EZJ_aJE
```

It reads your clipboard. Chapter headings come through, and panel labels like "In this video" are skipped. You can also save the text to a file and add the file name at the end.

`python yousnr.py build` regenerates every page. Use it after editing a template in `templates/`.

## Any video, on demand (`serve`)

Run this on a computer at home that stays on:

```bash
python yousnr.py serve
```

or double-click `start-yousnr.bat`, which restarts the server if it ever stops.

Someone opens `yousnr.com/watch?v=ANY_ID`. If that video isn't on the site yet, the page asks your computer, at `api.yousnr.com` through a Cloudflare Tunnel. Your computer fetches the captions from YouTube, groups them the way YouTube's transcript panel does, and sends the page back. That takes a few seconds. Every few minutes it also pushes new pages to GitHub, so after that they're static and instant for everyone, even with your computer off.

Safety limits (top of the server section in `yousnr.py`):
- one YouTube fetch at a time, at least 3 s apart
- 20 new videos per visitor per hour
- if YouTube says "too many requests", new fetches pause for 15 minutes
- videos with no captions are remembered for a day

Test locally first: run `serve`, then open http://localhost:8787/watch?v=265-EZJ_aJE.

### Cloudflare Tunnel (free), switched on and off with the server
1. Cloudflare dashboard → **Zero Trust** → **Networks → Tunnels** → **Create a tunnel** → Cloudflared → name it `yousnr`.
2. Install cloudflared: `winget install --id Cloudflare.cloudflared`
3. The dashboard shows an install command like `cloudflared.exe service install eyJhIjoi…`. **Don't run it.** Copy the long token (the part starting `eyJ`) into a new file called `tunnel-token.txt` in this folder. Pasting the whole command also works. Keep this file private: it's in `.gitignore`, so it never gets pushed.
4. **Public hostname** (newer dashboards call it *Published application routes*): subdomain `api`, domain `yousnr.com`, type `HTTP`, URL `localhost:8787` → Save.
5. Double-click `start-yousnr.bat`. The window shows `tunnel: connected, api.yousnr.com is live`. Check https://api.yousnr.com/api/health.

**On/off:** double-click `start-yousnr.bat` to turn it on, and close its window to turn it off (server and tunnel together). While it's off, everything already on yousnr.com still works; new videos show "the transcript server is offline". `python yousnr.py serve --no-tunnel` runs the server for this computer only.

### When YouTube rate-limits you
YouTube sometimes refuses plain caption downloads from a home connection ("429 Too Many Requests"), sometimes for hours. When that happens, the script opens the video in a hidden Chrome or Edge, turns CC on, and keeps the caption file the player loads. Real browsers keep getting captions during these blocks. Needs `python -m pip install --user playwright`. After one refusal it uses the player for the next hour without retrying the direct download.

`python yousnr.py remove <id>` deletes a video from the site, for example one saved from a bad paste.

### Line grouping
YouTube's transcript panel glues caption cues together until a line reaches about 100 characters (`PANEL_MIN_CHARS`), and stamps each line with its first cue's start time. That matches every panel line checked so far, for both auto-generated and creator captions. Some videos' panels use a newer transcription than the downloadable caption file. For those, the words can differ slightly; `add --browser` reads the panel itself.

## One-time setup

### 1. GitHub
1. Create a repo (e.g. `yousnr`) and push this folder to it.
2. Go to **Settings → Pages → Build and deployment**, choose *Deploy from a branch*, then `main` / `/docs`.
3. Under **Custom domain**, enter `yousnr.com`. The `docs/CNAME` file already says this.

### 2. Cloudflare DNS for yousnr.com
Add these records, and set the proxy to **DNS only (grey cloud)** for now so GitHub can issue the HTTPS certificate:

| Type  | Name  | Content |
|-------|-------|---------|
| A     | @     | 185.199.108.153 |
| A     | @     | 185.199.109.153 |
| A     | @     | 185.199.110.153 |
| A     | @     | 185.199.111.153 |
| CNAME | www   | `<your-github-username>.github.io` |

Wait until GitHub Pages shows the domain as verified, then tick **Enforce HTTPS**. After that you can switch the records to proxied (orange cloud) if you want. If you do, set Cloudflare **SSL/TLS → Full**, never Flexible, or you'll get a redirect loop.

### 3. Point yoursnr.com at yousnr.com (second domain)
In the **yoursnr.com** zone on Cloudflare:
1. Add DNS record `A @ 192.0.2.1` with **Proxied (orange cloud)**, plus the same for `www`. The address is a placeholder: Cloudflare answers the request itself, so it never goes there.
2. Go to **Rules → Redirect Rules → Create rule**, choose *All incoming requests*, then:
   - Type: *Dynamic*
   - Expression: `concat("https://yousnr.com", http.request.uri.path)`
   - Status code: 301
   - Preserve query string: on

Now `yoursnr.com/watch?v=ID` → `yousnr.com/watch?v=ID` → transcript.

## How YouTube's timestamps work (0:00, 0:06, 0:12…)

They are **not** fixed 6-second steps. Each line in YouTube's transcript panel is one caption cue from the video's caption track, and the timestamp is when that cue starts:

- **Auto-generated captions** are split by YouTube's speech recognizer into chunks of roughly one or two caption lines of text. At a steady talking pace that comes out to about 4–7 seconds each, which is why a fast, even talker shows near-regular 0:06, 0:12, 0:18. Pauses, music and slower speech make the gaps uneven.
- **Creator-uploaded captions** use whatever timing the creator set.
- The displayed time is **rounded down** to the whole second (a cue at 6.9 s shows `0:06`). It's `m:ss` below an hour and `h:mm:ss` after.
- **Chapter headings** are inserted before the first line that starts at or after each chapter's start time.

This site uses the same cue start times from the same caption track and formats them the same way, so the lines and stamps line up with YouTube's panel. The panel's own backend occasionally merges or splits a cue differently, so expect rare small differences.
