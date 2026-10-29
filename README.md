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
   `instagram_business_basic` + `instagram_business_content_publish` +
   `instagram_business_manage_insights` (the last one lets the bot learn from views and watch time),
   exchange it for a long-lived token. Put it and the IG user id in `.env`; the worker refreshes it
   weekly. A new token pasted into `.env` later replaces the refreshed copy automatically.
8. `DRY_RUN=false`, `reelbot check` until all green, `reelbot make` once by hand, then deploy.

### YouTube Shorts (optional, free)

Every reel can also go up as a YouTube Short right after Instagram (same file; a vertical video up
to 3 minutes is a Short). Uploading through the API costs nothing.

1. Make the channel (YouTube → Create a channel; a Brand Account keeps it separate from your name).
2. console.cloud.google.com → new project → APIs & Services → enable **YouTube Data API v3**.
3. OAuth consent screen: External, add the scopes `youtube.upload` and `youtube.readonly`, then
   **Publish app → In production**. In "Testing" Google kills the token after 7 days. For your own
   account no verification is needed: you'll click "Advanced → continue" past the unverified warning.
4. Credentials → Create OAuth client ID → **Web application**, authorised redirect URI
   `https://developers.google.com/oauthplayground`.
5. developers.google.com/oauthplayground → ⚙ → *Use your own OAuth credentials* → paste the client
   id and secret → scopes `https://www.googleapis.com/auth/youtube.upload
   https://www.googleapis.com/auth/youtube.readonly` → Authorize APIs (pick the channel) →
   *Exchange authorization code for tokens* → copy the **refresh token**.
6. Add `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` to `.env` / ENV_FILE.
   `reelbot smoke` shows the channel name. `reelbot youtube ID` posts a reel that went out before.

### Facebook Reels (optional, free)

The same reel also goes to a Facebook Page right after Instagram (Facebook pays Pages for reels
once they qualify for Content Monetization; uploading the clean file avoids the "repost" flag).

1. Create a Facebook Page for the series.
2. In your Meta app (developers.facebook.com → the app) add the use case **Manage everything on
   your Page**, with `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`.
3. Graph API Explorer → your app → *Get User Access Token* with those three permissions → pick the
   Page. ⓘ next to the token → *Open in Access Token Tool* → **Extend Access Token** → copy it.
4. Back in the Explorer, paste the long-lived token and run `GET me/accounts`: copy your Page's
   `id` and `access_token`. (The Access Token Debugger shows that Page token as "Expires: Never".)
5. Add `FB_PAGE_ID` and `FB_PAGE_TOKEN` to `.env` / ENV_FILE. `reelbot smoke` shows the Page name;
   `reelbot facebook ID` posts a reel that went out before.

## Deploy

**GitHub Actions (default, $0):** `.github/workflows/reelbot.yml` runs `reelbot tick` every 30 min
from 08:30 to 23:30 IST. Each run pulls the DB and in-flight renders from the private
`S3_STATE_BUCKET`, does one pass (Telegram buttons → generate if due → publish if due), pushes state
back. Put the whole `.env` into one repo secret named `ENV_FILE`. Kick off runs by hand from
Actions → reelbot → Run workflow: pick a command and, if it needs them, type its arguments in the
*args* box (`pick` → `Juno 3 Mochi 1`, `approve` → `2 --now`, `reject` → `1`, `regen` → `2 3`).
Every action done this way is also confirmed in Telegram. ~1 billed minute per idle run plus
~10–15 per reel keeps a private repo inside the free 2,000 min/month.

Telegram buttons are read by the next `tick` (scheduled every 30 min, or run one by hand). Each
tick's log ends with a line like `tick: Telegram: 2 handled · generate: not due · publish: nothing
due`, and every tap gets a reply in the chat, including when it fails.

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
reelbot approve ID [NOTE…] [--now]  approve; posts at schedule.publish_at, or now
reelbot reject ID [NOTE…]    reject; say why and the next episodes learn from it
reelbot note ID NOTE…        what worked / didn't in an episode (no ID: about the series)
reelbot lessons [--update]   what the bot has learned so far
reelbot stats                Instagram numbers of the posted reels
reelbot regen ID SHOT        re-render one shot (1-based) and re-cut
reelbot publish-due
reelbot run                  the worker
reelbot cast [NAME ...]      draw character-sheet options for Telegram
reelbot bakeoff [MODEL ...]  one episode on several video models; tap 🏆 on the best
reelbot retake ID [NOTE…]    re-render a one-take reel
reelbot pick NAME N [...]    lock in option N without Telegram, e.g. pick Juno 3 Mochi 1
```

## v3: how a reel is made (default)

Built from what the top accounts in this niche actually do (dyyhoang_aiker, kenai.film: frame-by-frame
and audio analysis of their reels, dyyhoang's published Seedance prompts, his Aithetica interview):

1. **Cast (once).** `cast` draws 3 character sheets per character; pick one (button or `pick Juno 3`).
2. **Storyboard built backward from the winning move.** The planner first picks the one decisive
   action that beats the boss, then builds hook (stakes) -> escalate (the boss is huge) -> clash ->
   climax (the winning move) -> twist. One continuous scene: every shot starts where the last ended
   ("ends"), screen direction is kept, one recurring motif ties the cuts together.
3. **Dialogue bookends** (`take.dialogue: bookends`): up to 3 short lines in the set-up and one
   deadpan line at the twist; the middle is wordless action. That's the format of the 200K-like and
   12K-like reels we studied. Lines get our own burned-in subtitles; the music ducks under them.
4. **One generation for the whole reel**, cuts inside it (no more stitched 3 s clips):
   - Kling 3.0: starts from a composed opening frame (cast + location painted together), native
     multi-shot list, native sound.
   - Seedance 2.0: same opening frame, cuts written into the prompt, native audio.
   - Seedance 2.5 (what those accounts use): location plate + character sheets as references,
     sectioned prompt in the same layout as their published prompts.
5. **Edit:** music-led mix over the model's own sound effects, music drops before the climax and
   slams back, a flash frame plus a black-and-white anime impact frame (speed lines) on the climax
   cut, one quiet beat before the twist, yellow anime subtitles, -14 LUFS. The music ducks only
   while a line is actually spoken and stays up to the last frame (the top reels never fade out
   their payoff; their cuts follow the action, not the beat, and the music's big lift lands on the
   story's turn).
6. **Craft details from the top reels** (INK: 435K, 238K and 14K likes, studied frame by frame):
   open on motion or a question-asking detail, never a static wide; spoken set-up as shot /
   reverse shot, phone calls as a split screen; the irony cut ("Don't worry" -> smash cut to the
   opposite); the effect escalates in 2-3 steps of scale with a cut back to the calm cause; anime
   reaction effects in the picture; one still beat before the punchline; end on a face to camera.
   The vibe-only reel without a stake got 17-30x fewer likes than the two with one.

**Bake-off:** `bakeoff` plans one episode and renders it on every model in `take.bakeoff` (or the
ones you type in the args box: `kling3 seedance2 seedance25`). Storyboard, plate, opening frame and
music are shared, so the only difference is the model. Tap 🏆 on the best one: it becomes the model
for every reel and the others are rejected. 🔁 Retake re-renders one reel's take.

## How it gets better over time

Every reel teaches the next one:

- **Your reviews.** Approve / reject / retake are recorded. Say why and it counts much more:
  Actions → `reject` with args `7 the payoff made no sense`, or `note` with `7 opening too static`.
  In Telegram: send `#7 opening too static`, or reply to any message about #7 (replying to the
  review video itself still edits its caption). `/note …` without a number is about the series.
- **Viewers.** Once a day for two weeks after posting, the bot reads each reel's Instagram numbers:
  views, how much of the reel people watch on average, skip rate, shares, saves.
- **Lessons.** An LLM keeps a notebook of at most 10 concrete lessons drawn from that evidence (your
  notes outrank the numbers; numbers from fewer than 3 reels are only an early signal). Every new
  storyboard is written with the lessons plus the best- and worst-watched episodes so far.
  See them with `lessons` or `/lessons`; a note that changes them is answered with the new lesson.
- **Weekly report** (Sunday evening, Telegram): views, % watched vs last week, best reel, lessons.

## v2: how a reel is made

1. **Cast (once).** `reelbot cast` (or send `/cast` to the bot) draws 3 character-sheet options per
   character with Grok Imagine 2.0. Tap ✅ on the one you like (or run `pick`, e.g. `Juno 3 Mochi 1`).
   Episodes wait until every character
   has an approved sheet; the sheet is fed to every shot as a reference image.
2. **Storyboard.** The planner fills a fixed recipe: 5 beats (hook → escalate → clash → climax →
   twist), one shot size each (never the same twice in a row, at least one close and one wide), one
   camera move, one action, one location. Plans that break a rule are sent back with the reason.
3. **Location plate.** One empty-environment image per episode, shared by all shots.
4. **Shots.** Kling 4.0 Reference-to-Video, 5 × 3 s, 9:16, 720p. Each prompt is composed by code:
   size + angle + camera, `@image_N` cast refs + one action, setting + light, style lock, negatives.
5. **Edit + cover.** ffmpeg cut with music; the title goes on a poster cover (climax frame + title),
   uploaded as the Reels cover, so the hook shot stays clean.

## Cost (one 15 s reel, list prices; Higgsfield often runs ~30% below these)

| Model | Per reel | 3 a week |
|---|---|---|
| Kling 3.0 (opening frame + take) | ~$1.90 | ~$25/month |
| Seedance 2.0 | ~$4.70 | ~$61/month |
| Seedance 2.5 | ~$8.50 | ~$110/month |

The guard stops at `budget.monthly_usd` ($40) and `budget.per_reel_usd` ($9). Pick the model by eye
in a bake-off, then set reels/week to fit the budget.

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
