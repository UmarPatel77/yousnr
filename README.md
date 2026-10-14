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
5. **Always on** (optional): `python yousnr.py autostart on` (or double-click `yousnr-on.bat`). No window to keep open; see *Always on* below.

Commands:

| Command | What it does |
|---|---|
| `python yousnr.py add <video>` | add a video, exactly as YouTube's transcript panel shows it (`--show` to watch, `--captions` for the faster caption-file version) |
| `python yousnr.py correct <video> [--full]` | fix likely mistakes with speech recognition (runs automatically for new videos) |
| `python yousnr.py upgrade [--dry-run]` | bring older videos up to the current checking rules (the server also does this in the background; layout-only updates are free) |
| `python yousnr.py usage` / `bakeoff <video>` | spending this month / compare the engines on your videos |
| `python yousnr.py remove <video>` | delete a video from the site |
| `python yousnr.py serve` | on-demand server + tunnel in this window (what `start-yousnr.bat` runs); close to turn off |
| `python yousnr.py autostart on` / `off` | always on: runs with no window and turns on by itself when you sign in to Windows |
| `python yousnr.py start` / `stop` / `status` | turn the background server on / off now, or see if it's on (`yousnr-on.bat`, `yousnr-off.bat`, `yousnr-status.bat`) |
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

or double-click `start-yousnr.bat`, which restarts the server if it ever stops. To run it without a window, see *Always on* below.

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

**On/off:** double-click `start-yousnr.bat` to turn it on, and close its window to turn it off (server and tunnel together). Or run it with no window at all (*Always on*, next). While it's off, everything already on yousnr.com still works; new videos show "the transcript server is offline". `python yousnr.py serve --no-tunnel` runs the server for this computer only.

### Always on (no window)

```
python yousnr.py autostart on
```

This turns YouSNR on now, in the background, and puts a shortcut in Windows' Startup folder so it turns on by itself every time you sign in. There's no window to keep open. The server, the tunnel, the corrections and the GitHub pushes all keep running, and if the server ever crashes it's started again within seconds.

| | |
|---|---|
| `python yousnr.py status` | on or off, tunnel connected, reachable from the internet, spending this month, last lines of the log |
| `python yousnr.py stop` | turn it off now. It finishes a paid word check that's under way and publishes waiting pages first |
| `python yousnr.py start` | turn it back on now |
| `python yousnr.py autostart off` | stop turning on at sign-in (it's also listed in Task Manager → Startup apps) |

The `.bat` files do the same with a double-click: `yousnr-on.bat` (autostart on), `yousnr-off.bat`, `yousnr-status.bat`.

What it would have printed goes to `yousnr.log` in this folder, with the date and time on each line (renamed to `yousnr.log.old` past 5 MB). Only one YouSNR runs at a time: if the background one is on, `start-yousnr.bat` says so and stops. Process IDs live in `run/`.

**The computer has to stay on and awake.** Sleep or hibernate means it's off: no server, no tunnel, no word checks. Pages already on yousnr.com keep working; new videos show "server offline" until it wakes, and then YouSNR picks up where it left off by itself.

While plugged in, YouSNR stops Windows from sleeping on its own (the screen can still turn off). On battery it doesn't interfere, so a laptop never runs itself flat. To keep a laptop running with the lid closed while plugged in, and never sleep or hibernate on its own while plugged in:

```
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

Choosing Sleep or Hibernate yourself still works. If the laptop has a battery charge limit (often called Conservation mode, Battery Health Charging or similar, in the maker's app), turn it on so being plugged in all the time doesn't wear the battery.

After a restart (Windows updates), YouSNR comes back when you sign in. Settings → Accounts → Sign-in options → *Use my sign-in info to automatically finish setting up after an update* lets Windows sign back in by itself after update restarts. For a dedicated mini PC, also turn on the BIOS setting *Restore on AC power loss* (or *Power On after power failure*) so it starts again after a power cut.

Your API keys are read from your Windows user settings (what `setx` saved), so the background version finds them even if you set them after signing in.

### On a free cloud server instead (Oracle Cloud + Cloudflare WARP)

So your laptop can sleep. Oracle's Always Free tier gives you a small ARM server for free (2 cores, 12 GB since June 2026). YouTube blocks data-centre addresses, so the server sends **only its YouTube traffic** through Cloudflare's free WARP (proxy mode). Everything else (SSH, GitHub, the speech APIs, the tunnel) goes direct. Whether YouTube lets WARP through can change at any time: `check-youtube` tells you.

**Only one place runs YouSNR at a time.** Both would answer api.yousnr.com and push to GitHub. Turn the laptop's off before turning the server's on.

1. **Server.** Sign up at oracle.com/cloud/free (home region: Canada Southeast (Toronto); it can't be changed later). Then Compute → Instances → Create instance: image *Canonical Ubuntu 24.04*, shape *Ampere VM.Standard.A1.Flex* with 2 OCPUs and 12 GB, *Generate a key pair for me* → **Save private key**. Create, then copy the *Public IP address*. "Out of capacity" means try again later.
2. **Connect** from PowerShell: `ssh -i <the .key file> ubuntu@<public IP>`
3. **Set up** (about 10 minutes):
   ```
   git clone https://github.com/UmarPatel77/yousnr.git ~/yousnr
   bash ~/yousnr/deploy/oracle-setup.sh
   ```
   It installs everything, connects WARP in proxy mode, runs `check-youtube`, and prints a deploy key.
4. **GitHub deploy key:** repo → Settings → Deploy keys → Add deploy key, paste the key it printed, tick *Allow write access*. This key can push to this repo only.
5. **API keys:** `nano ~/.config/yousnr.env` and paste them after the `=` signs (private file, outside the project).
6. **Switch over.** On the laptop: `python yousnr.py stop` and `python yousnr.py autostart off`. Then copy the tunnel token and this month's spending to the server:
   ```
   scp -i <key> tunnel-token.txt ubuntu@<IP>:yousnr/
   scp -i <key> data/_usage.json data/_views.json ubuntu@<IP>:yousnr/data/
   ```
   On the server: `source ~/.bashrc`, `yousnr autostart on`, `yousnr status`.

On the server, `yousnr` is a shortcut for `python yousnr.py` (with the right Python). `yousnr status | stop | start | check-youtube` work as on Windows; the log is `~/yousnr/yousnr.log`.

**Updates:** apply the zip on the laptop and push as usual, then run `bash ~/yousnr/deploy/update.sh` on the server (from PowerShell: `ssh -i <key> ubuntu@<IP> "bash ~/yousnr/deploy/update.sh"`).

**If YouTube blocks WARP** (`check-youtube` fails steps 1–4): get a new WARP address with `warp-cli disconnect; warp-cli registration delete; bash ~/yousnr/deploy/oracle-setup.sh` (the setup puts WARP back in proxy mode before connecting, so SSH keeps working). If that keeps failing, a paid residential proxy works the same way: put its address in `YOUSNR_PROXY` in `~/.config/yousnr.env` (e.g. `http://user:pass@host:port`), or go back to the laptop (`python yousnr.py autostart on`).

**Oracle may reclaim idle free servers** (very low CPU, network and memory over 7 days). If you get that email, upgrade the account to Pay As You Go: Always Free resources stay free.

### When YouTube rate-limits you
YouTube sometimes refuses plain caption downloads from a home connection ("429 Too Many Requests"), sometimes for hours. When that happens, the script opens the video in a hidden Chrome or Edge, turns CC on, and keeps the caption file the player loads. Real browsers keep getting captions during these blocks. Needs `python -m pip install --user playwright`. After one refusal it uses the player for the next hour without retrying the direct download.

`python yousnr.py remove <id>` deletes a video from the site, for example one saved from a bad paste.

### Corrections (speech recognition on top of YouTube's text)
YouTube's transcript is the starting point; YouSNR then fixes words it most likely got wrong, keeping YouTube's lines, timestamps and chapters. Corrected words have a faint highlight (hover to see YouTube's word), and the **YouTube's original** switch shows YouTube's exact text.

- **Every new video (selective, a few cents):** lines where YouTube's two recognizers disagree (transcript panel vs. caption file), plus risky words (unusual acronyms, rare words, names not in the title/description), are cut from the audio and sent to **ElevenLabs Scribe v2** and **AssemblyAI Universal-3.5 Pro**, with key terms taken from the title, description and chapters. A word changes only when at least two sources agree on the change and they outweigh the sources that back YouTube. One engine's slip is never enough.
- **Bleeps:** YouTube's auto-transcripts replace swear words with `[ __ ]`. Those lines are always checked first, and the word the engines hear is put back. YouTube gets no vote there, since it never knew the word, so one engine is enough, and the more accurate one wins if they disagree.
- **Popular videos (full):** after `YOUSNR_FULL_AT_VIEWS` views (default 20), the whole audio goes to both engines and the same vote runs over every word.
- **Budget:** `YOUSNR_MONTHLY_BUDGET` (USD, default 5). Work that would go over it is skipped. `python yousnr.py usage` shows spending.

Setup (once): put the keys in your Windows account, never in this folder, then open a new window:
```
setx ELEVENLABS_API_KEY "..."
setx ASSEMBLYAI_API_KEY "..."
winget install DenoLand.Deno            # helps yt-dlp download YouTube audio
python -m pip install --user -U -r requirements.txt
```
Commands: `python yousnr.py correct <video> [--full]`, `python yousnr.py bakeoff <video> [--minutes 5]` (compares the engines with YouTube on a stretch of a video), `python yousnr.py usage`. `add --no-correct` skips corrections. Optional settings: `setx YOUSNR_MONTHLY_BUDGET 10`, `setx YOUSNR_FULL_AT_VIEWS 50`, `setx YOUSNR_PRIMARY assemblyai`.

### Upgrades for older videos
Each improvement to the checking rules gets a version (v1 first check, v2 restore bleeped words, v3 keep tags like `[music]` in place and split a fix that spans two lines; v3 is free because it only lays out the saved fixes again). Every video remembers which version it was checked with. When the rules improve, the server upgrades older videos in the background:
- **Only what changed is paid for.** A rule says which videos it affects; v2 only touches videos with `[ __ ]`, and only those lines. Earlier fixes are kept. Videos a rule doesn't affect get their version updated for free.
- **Videos being read go first.** When someone opens an outdated page, it jumps the queue.
- **The rest go slowly:** `YOUSNR_UPGRADES_PER_HOUR` (default 6), most-viewed first.
- **The budget is respected.** When the monthly budget runs out, upgrades pause until the 1st of next month.

`python yousnr.py upgrade --dry-run` shows how many videos need what, and the estimated cost. `python yousnr.py upgrade [--limit N]` runs them now.

### No music videos
Videos YouTube flags as music (official music videos and songs), videos in the "Music" category, and auto-generated "- Topic" artist channels are skipped before anything is fetched. Visitors see "Music videos aren't on YouSNR", and the server remembers that for 30 days. `add --allow-music` overrides it for one video.

### Where the text comes from
By default YouSNR reads **YouTube's own transcript panel** ("Show transcript" → *In this video* → Transcript). It opens the video in a hidden Chrome window (YouSNR's own profile, see `signin`), clicks *Show transcript*, and keeps YouTube's reply (`youtubei/v1/get_panel`): the same lines, timestamps and chapter headings you see on YouTube. If a hidden window doesn't work, it retries once with a normal window parked off-screen.

Since 2026 the panel often shows a newer transcription than the downloadable caption file (different words, different line breaks), so the panel is the only exact source.

If the panel can't be read, it falls back to the caption file, glued into ~100-character lines the way the old panel did (`PANEL_MIN_CHARS`). If YouTube rate-limits caption downloads, it gets them through the video player instead. `--captions` skips the panel and uses the caption file only, which is faster.

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
