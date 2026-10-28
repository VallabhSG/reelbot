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
| Higgsfield, Kling, PixVerse, Dreamina (Seedance/Seedream) | image + video generation | **Higgsfield API** — one async API for Soul, Kling, Seedance, PixVerse, Wan… |
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
```

## Cost (budget tier, 3 × 5s shots)

| Item | Price used | Per reel |
|---|---|---|
| Soul keyframe × 3 | $0.0032 / image | ~$0.01 |
| Kling 2.5 i2v × 15s | $0.042 / s | $0.63 |
| LLM plan | Groq free tier | $0 |
| **First take** | | **~$0.64** |

Daily posting ≈ $20/month plus regenerations, under the default $40 cap. Seedance 2.5
(reference-to-video, better character consistency) is a commented preset in the series file; at
Seedance 2.0's Higgsfield rate (~$0.93/s) a 15s reel is ~$14, so switch when revenue covers it.
Prices drift: update `cost_*` in the YAML from your Higgsfield dashboard.

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
