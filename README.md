# reelbot

Automated AI short-film Reels, in the format of accounts like @dyyhoang_aiker: a recurring series,
one wordless 15-second anime micro-film per day, a bold title card, a two-line hook caption, music.

```
 schedule (10:00)                                                     you, on Telegram
      │                                                                     │
      ▼                                                                     ▼
 LLM planner ──► Higgsfield: Soul keyframes ──► Higgsfield: Kling i2v ──► preview ──► ✅ / 🔁 shot / 🎵 / ❌ / reply = new hook
 (series bible,     (+ character refs)           (Seedance 2.5 later)        ▲                │
  calendar)                                                                   │                ▼
                      ElevenLabs music ──► ffmpeg: 9:16 cut, title card, mix ─┘     R2 public URL ──► Instagram Reels API (19:30)
```

Every paid call goes through a budget guard (monthly + per-reel caps). State is SQLite; a crash
mid-render resumes without re-paying for finished shots.

## His stack → this pipeline

| His bio / credits | Role | Here |
|---|---|---|
| Higgsfield, Kling, PixVerse, Dreamina (Seedance/Seedream) | image + video generation | **Higgsfield API**: Grok Imagine 2.0 (cast sheets, plates) + Kling 4.0 Reference-to-Video |
| CapCut ("Made with Seedance on CapCut") | editing | **ffmpeg** + Pillow title cards (CapCut has no public API) |
| ElevenLabs ("background music with @elevenlabs") | music | **ElevenLabs Music API** |
| Pollo, ImagineArt, Flova, YouArt | other sponsored tools | swappable: point `models.*.endpoint` at any Higgsfield model, or add a provider |

The `CPP:` line in his bio is creator partner programs: those tools sponsor him. That's the
monetization path once you have reach: credit the tool you used in every caption from day one.

## Quick start (no keys, no spend)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,music]"
cp .env.example .env            # DRY_RUN=true by default
reelbot check
reelbot make                    # stub images/video/music, real ffmpeg -> data/jobs/1/final.mp4
reelbot approve 1 --now         # "publishes" to the dry-run publisher
pytest
```

## Going live

1. **Series** — copy `series/example.yaml`, write your premise, style bible, calendar, handle.
   Drop a display font you're licensed to use at `assets/fonts/title.ttf`.
2. **Higgsfield** — create an API key at console.higgsfield.ai (key id + secret, shown once), top up
   ~$10. Open the Kling 2.5 image-to-video model page and paste its endpoint ID and input field names
   into `models.video`; check the Soul page for the aspect-ratio field name in `models.image`.
   Those IDs aren't in Higgsfield's public docs index, which is why they're config, not code.
3. **LLM** — any OpenAI-compatible endpoint. Groq's free tier is enough for one plan a day.
4. **Music** — ElevenLabs Music API needs a paid plan. To start at $0, set `music.provider: library`
   and put tracks you have rights to in `assets/music/`.
5. **Storage** — Cloudflare R2 bucket with public access (r2.dev URL or custom domain) + an API
   token with object read/write. Instagram pulls the MP4 from there.
6. **Telegram** — `@BotFather` → token. Message your bot once, then read your chat id from
   `https://api.telegram.org/bot<token>/getUpdates`. Only that chat can approve.
7. **Instagram** — switch the account to Professional (Creator). In a Meta developer app add
   *Instagram API with Instagram Login*, add the account as a tester, generate a token with
   `instagram_business_basic` + `instagram_business_content_publish`, exchange it for a long-lived
   token. Put it and the IG user id in `.env`; the worker refreshes it weekly.
8. `DRY_RUN=false`, `reelbot check` until all green, `reelbot make` once by hand, then deploy.

## Deploy

**GitHub Actions (default, $0):** `.github/workflows/reelbot.yml` runs `reelbot tick` every 30 min
from 08:30 to 23:30 IST. Each run pulls the DB and in-flight renders from the private
`S3_STATE_BUCKET`, does one pass (Telegram buttons → generate if due → publish if due), pushes state
back. Put the whole `.env` into one repo secret named `ENV_FILE`. Kick off runs by hand from
Actions → reelbot → Run workflow (`smoke`, `make`, `status`). ~1 billed minute per idle run plus
~10–15 per reel keeps a private repo inside the free 2,000 min/month.

**Always-on host (instant button response):**

```bash
docker build -t reelbot .
docker run -d --env-file .env -v reelbot-data:/app/data --restart unless-stopped reelbot
```

Any always-on host with a persistent volume works (a small VPS, Railway, Fly.io). It's one
process: schedule, Telegram long polling, publishing. No inbound ports.

## Commands

```
reelbot check                validate config, cost estimate, credentials
reelbot make [--job ID]      plan + render one now (or resume a failed job)
reelbot status               recent jobs and month spend
reelbot approve ID [--now]   approve; posts at schedule.publish_at, or now
reelbot reject ID
reelbot regen ID SHOT        re-render one shot (1-based) and re-cut
reelbot publish-due
reelbot run                  the worker
reelbot cast [NAME ...]      draw character-sheet options for Telegram
```

## v2: how a reel is made

1. **Cast (once).** `reelbot cast` (or send `/cast` to the bot) draws 3 character-sheet options per
   character with Grok Imagine 2.0. Tap ✅ on the one you like. Episodes wait until every character
   has an approved sheet; the sheet is fed to every shot as a reference image.
2. **Storyboard.** The planner fills a fixed recipe: 5 beats (hook → escalate → clash → climax →
   twist), one shot size each (never the same twice in a row, at least one close and one wide), one
   camera move, one action, one location. Plans that break a rule are sent back with the reason.
3. **Location plate.** One empty-environment image per episode, shared by all shots.
4. **Shots.** Kling 4.0 Reference-to-Video, 5 × 3 s, 9:16, 720p. Each prompt is composed by code:
   size + angle + camera, `@image_N` cast refs + one action, setting + light, style lock, negatives.
5. **Edit + cover.** ffmpeg cut with music; the title goes on a poster cover (climax frame + title),
   uploaded as the Reels cover, so the hook shot stays clean.

## Cost (3 reels/week)

| Item | List price | Per reel |
|---|---|---|
| Location plate (Grok Imagine 2.0) | ~$0.04 / image | $0.04 |
| Kling 4.0 R2V, 5 × 3 s @ 720p | $0.157 / s ($0.11 on promo) | $2.36 ($1.65) |
| LLM plan | Groq free tier | $0 |
| **First take** | | **~$2.40** |

3 reels a week ≈ $31/month at list price, under the $40 cap; a shot redo costs ~$0.47. Cast sheets
are a one-time ~$0.24. Seedance 2.5 (what the reference account credits) is ~$4.85–6.93 per 15 s at
720p; switch `models.video` when revenue allows.

## What the reference account does that you should copy

- **One series, numbered** ("Day 39 / 100"): gives people a reason to follow, not just like.
- **Fixed title-card template** on every cover: the grid reads as a brand.
- **Hook captions**: one or two lines that open a curiosity gap; credits and hashtags after.
- **Collab posts** with AI aggregator pages for reach (do this manually; it's relationship work).
- **Expect outliers**: one of his reels sits around 422K likes while most are in the hundreds to
  low thousands. Consistency is what gives the outlier a chance.

## Known gaps

- Higgsfield model IDs/fields for Kling and Soul must be copied from the console (see step 2).
- I couldn't confirm an API parameter for Instagram's "AI info" label. Captions carry
  `#aigenerated`; check the first few posts and add the label in-app if it isn't applied.
- Don't clone his look: own title font, own series, no copyrighted characters or music.
