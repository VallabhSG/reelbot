"""LLM planner: series bible + episode number -> a validated, rule-checked storyboard.

The LLM only fills structured fields (beat, shot size, angle, camera, cast, one action, light);
storyboard.compose_shot_prompt turns them into model prompts with a fixed formula, so prompt
quality doesn't depend on the LLM's prose.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from .models import BEATS, SHOT_KEYS, EpisodePlan, recover_shots
from .providers import JSONChat
from .providers.llm import AnswerCutOff
from .series import EpisodeSeed, Series
from .storyboard import normalise, storyboard_problems

log = logging.getLogger(__name__)

AUTO = object()  # plan()/user_prompt(): use the numbered calendar entry for this episode

SYSTEM = """You are the director of a wordless anime micro-film series for Instagram Reels.
Each episode is {seconds} seconds: exactly {n} shots of {shot_s} seconds, then music. No dialogue,
no on-screen text: the story must read from the pictures alone.

SERIES: {name}
PREMISE: {premise}
RECURRING CAST (reference images are supplied automatically; never describe their looks):
{characters}

STORYBOARD RULES
- Beats, in this exact order: {beats}.
  hook = an instantly readable situation with stakes in the first second;
  escalate = the character commits, tension rises;
  clash = the big dynamic action, energy, motion;
  climax = the decisive instant, often a close or slow-motion moment;
  twist = a deadpan, funny anticlimax that recontextualises everything.
- Shot sizes: EWS, WS, MS, MCU, CU, ECU. Never the same size twice in a row. At least one CU or ECU
  and at least one WS or EWS. Vary angles: eye level, low angle, high angle, overhead, POV, dutch angle.
- camera: exactly ONE physical move per shot, e.g. "static locked-off", "slow push-in", "fast push-in",
  "pull-back reveal", "lateral tracking", "low tracking", "orbit", "crane up", "handheld follow",
  "whip pan", "slow-motion speed ramp" (only on climax).
- action: ONE action with a concrete verb and a visible end state, max 25 words. Refer to cast by name
  only. No dialogue, no thoughts, no text, no signs.
- cast: names from the cast list in that shot (max 2), [] for an empty shot.
- ONE location for the whole episode. location.plate_prompt describes that place EMPTY (no people, no
  animals), with time of day, weather and light, and blank, unreadable signage.
- light: direction + colour temperature, consistent across shots (same time of day).
- hook (the caption, not a shot): 1-2 short lines, curiosity gap, no hashtags.
- music_prompt: genre, mood, tempo, instruments, "instrumental". Never real artists or songs.
- Never use copyrighted characters, brands, logos, real people or real teams.

Respond with JSON only:
{{"title": str (1-2 words, max 14 chars), "category": str, "emoji": str, "logline": str, "hook": str,
  "location": {{"name": str, "plate_prompt": str}},
  "shots": [{{"beat": str, "size": str, "angle": str, "camera": str, "cast": [str], "action": str,
              "light": str}}, ... exactly {n}],
  "music_prompt": str}}"""


DIALOGUE_NONE = """- line / speaker: always "". The episode is wordless: the story reads from the pictures."""

DIALOGUE_BOOKENDS = """- DIALOGUE BOOKENDS (the format of the most-watched reels in this niche): a few very short spoken
  lines set up the stakes at the start, the middle is pure action with sound effects and music,
  and one short line (deadpan, or smug after a win) lands the punchline in the twist.
  line: at most {max_lines} lines in the whole episode, only on hook/escalate shots (set-up) and the
  twist shot (punchline). Max 10 words each, everyday spoken English, no names of brands. Every
  other shot: line "" and speaker "".
  speaker: who says it, a character IN that shot who can talk: {speakers}. {mute}
  Stage a set-up line as shot / reverse shot: over the listener's shoulder onto the speaker's face,
  then the listener's reaction in close-up (a blush, wide eyes, a gulp). Stage a phone call as a
  diagonal split screen: the caller on one side, the other person on the other, both visible.
  IRONY CUT: a set-up line that promises one thing ("Don't worry, I'll be careful.") followed by a
  smash cut to the exact opposite is the strongest laugh in this format.
  Lines must work for any viewer; the pictures still carry the story."""

CRITIC_PASS = 7
CRITIC_SYSTEM = """You are a FIRST-TIME VIEWER scrolling Instagram Reels with the sound off. You will
only see what is listed: the opening frame and each shot's picture (plus subtitles for any spoken
line). You know nothing else about the story.

Answer honestly, as a viewer, not as a writer:
- summary: one sentence, what happened and what the joke was.
- confusions: anything you could not understand from the pictures: an action you can't picture,
  an object or power that appears from nowhere, a cut where you lose track of who/where, a
  punchline that doesn't land, AND anything that makes no sense: the problem already solved at the
  start (the fix already in place or within reach in the place or the first frame), a prop that
  changes place or state with nobody touching it, an object that has nothing to do with this
  story. Empty list if none.
- clear: true only if you could retell the cause, the decisive action and the punchline.
- score: 1-10 clarity.

JSON only: {"summary": str, "confusions": [str], "clear": bool, "score": int}"""

ENDING_TRIUMPH = (
    "ENDING for this episode: TRIUMPH. The twist beat is the hero's big, satisfying win landing in "
    "front of someone who doubted her or is watching (a jaw drop, a drink spilling), then her smug "
    "face turned to the camera. No deadpan defeat this time."
)
ENDING_DEADPAN = "ENDING for this episode: DEADPAN. The twist is the deadpan anticlimax."


def wins_this_episode(series: Series, episode_no: int) -> bool:
    """`take.endings: alternate`: even-numbered episodes end in a triumph, odd ones deadpan."""
    return series.take.endings == "alternate" and episode_no % 2 == 0


SYSTEM_TAKE = """You are the director of an anime micro-film series for Instagram Reels.
Each episode is ONE continuous {seconds}-second scene in exactly {n} shots with hard cuts, rendered
in a single generation, then cut to music. No narration and no on-screen text.

SERIES: {name}
PREMISE: {premise}
RECURRING CAST (reference images are supplied automatically; never describe their looks):
{characters}

METHOD (how the most-watched creator in this niche works)
- First pick the WINNING MOVE: the one decisive action that beats today's boss. Build the whole
  episode backward from it. hook = the goal and why it matters right now (stakes in one glance);
  escalate = how strong or unfair the boss is (show contrast: the small hero vs a huge problem,
  staged like a dangerous arena); clash = the fight; climax = the winning move itself (slow motion or an
  extreme close-up); twist = the deadpan anticlimax, or the TRIUMPH when the episode asks for one.
  One simple idea, every shot serves it.
- Pace: slow, then faster, a burst on the winning move, one quiet beat, then the twist.
- The stake is social and relatable, a feeling everyone knows: someone is waiting, a message just
  arrived, a crush is watching, a parent is calling, the last one is about to go. A quick phone call
  or text exchange is a great hook.
- Make the feeling PHYSICAL: blow the tiny everyday emotion up into anime spectacle (a shockwave that
  cracks the pavement, speed lines, flying across the city, slow-motion debris, a crater). The bigger
  the gap between the small cause and the epic effect, the funnier. End on one clear payoff image.
- ESCALATION LADDER: the effect grows in 2-3 steps, each one bigger than the last (cracks under the
  feet -> a crater -> a shockwave rolling down the whole street; or through one wall -> through the
  next building -> out over the bay). Between two steps, cut back once to whoever caused it, perfectly
  calm while the world breaks: that contrast is the joke.
- On the biggest emotional reaction put an anime effect IN the picture: a radial colour burst
  behind the face, heart eyes, a giant sweat drop, steam from the ears.
- THE CHALLENGER'S ENTRANCE: early on, one low-angle extreme close-up of the hero gearing up like a
  fighter before a boss (cracking her knuckles, a glint in her narrowed eyes, her sneakers planting
  on the ground; never by touching her clothes or hair), then cut wide. One second is enough. A
  strong version is a REFLECTION that holds both fighters in one frame: the boss reflected in her
  eyes, or her face
  reflected in the boss itself (the phone's black screen, the microwave door, the train window).
  Another is an EYE MATCH CUT: shot 1 ends on the problem's round shape (the battery ring, the
  clock face, the circled F) centred in the upper third, one third of the frame wide; the next shot
  is an extreme close-up of the hero's eye with the iris in exactly that spot and size, the problem
  reflected in her pupil as the boss-mode light flares. Write that position and size in words in
  BOTH shots.
- THE TURN IS AN INSERT: the winning move starts on a tight insert of a hand on the one object that
  matters (a thumb pressing the key fob, fingers closing on the plug, a finger over the snooze
  button), then cut wide to what it causes.
- COLOUR CODE THE FIGHT: the opening is ordinary flat daylight or room light; the moment the boss
  "powers up", the light turns to boss mode (saturated red or cyan rim light, long shadows, the
  background darkening); the last shot snaps back to the flat light of the opening. Name the light
  in each shot's description.
- A WITNESS SELLS IT: right after the winning move, one shot of someone watching it (Mochi, a
  bystander, Ren when he is in the episode) reacting big: jaw drop, a drink spilling, glasses
  sliding down the nose. The audience feels the payoff through them. When shots are short, this
  witness can be the twist shot itself (Mochi's deadpan stare).
- Comic timing: one beat of stillness right before the punchline (a held empty frame or a deadpan
  stare), then the payoff. The last shot ends on a face turned toward the camera that shows the
  feeling (a heart-eyes grin, a smug unimpressed stare).

HOW THE BEST REELS IN THIS FORMAT ARE BUILT
- The set-up is one continuous scene in ONE location, in story order, no time jumps. Every shot
  starts exactly where the previous one ended: "ends" is the visible state at the cut (position,
  pose, what is in hand), and the next shot's action continues from it. Cut on the action (match
  cuts), never cut to something unrelated. The escalation may break OUT of the location (through a
  wall, up into the sky, across the city) when the force carries a character there: keep it
  continuous with the same framing, direction and light (the flying hero stays centred, rushing at
  the camera, while the places behind change).
- BLOCKING: say where the goal is relative to the character in the opening frame ("the open door
  right beside her, screen right") and keep it in every shot. The character moves straight at the
  goal by the shortest path, and goes THROUGH an opening head-on (a dive into a train doorway goes
  from the platform into the car, across the train, never along it).
- Keep screen direction: whoever moves, moves the same way on screen in every shot ("direction").
- Short shots: {lo}-{hi} seconds each, summing to {seconds}. Fast shots (1-2 s) in escalate/clash,
  the climax may hold 3-4 s (slow motion). Whole seconds only.
- Mix scales like a film editor: establishing wide, medium, and at least one detail INSERT
  (hands, an object, feet, eyes: CU or ECU). Never the same size twice in a row. At least one CU/ECU
  and one WS/EWS.
- motif: ONE small object or colour (e.g. "a red hair tie", "the green exit light") that appears in
  at least two shots and pays off in the climax or twist. It ties the cuts together. It lives on a
  story prop or a character (a phone screen, a hair tie), never as a poster or wall decoration.
- layout: the fixed geography of the location, 30-60 words: where every prop that matters is and
  how far apart ("the wall outlet is low on the LEFT wall, two metres from the sofa; the charger
  cable is one metre long, too short to reach the sofa; the floor lamp stands right of the sofa").
  Every shot keeps it; the action never moves a prop that isn't picked up on screen.
- start_state: 30-60 words: where every prop the story turns on starts, and in what state, before
  anything happens ("the phone is in Juno's hand at 2%; the charger lies coiled on the bookshelf
  across the room, unplugged; the outlet on the left wall is empty"). THE PROBLEM IS STILL UNSOLVED
  IN THE FIRST FRAME: whatever fixes it starts out of reach, switched off, unplugged, closed or
  missing, never already in place or within easy reach. If the joke is that the fix was there all
  along, it starts hidden from the hero (behind her, under the cat) and the camera shows it only at
  the twist. The location, the layout and the opening frame all match it.
- opening_frame: the very first frame as a still image: where each character is, pose, what is in
  frame, framing. It must match shot 1 and show EVERY character who appears anywhere in the episode
  (a small background presence is fine): the video is generated from this frame.

STORYBOARD RULES
- Beats in order, each at least once, never going backwards: {beats}.
  hook = an instantly readable situation with stakes in the first second. Shot 1 starts ON MOTION
  or on a detail that asks a question (feet stepping out of a doorway, a hand grabbing keys, a
  phone lighting up at 2%, a clock at 6:59, a red F on a test). Never open on a static wide of
  someone just standing;
  escalate = the character commits, tension rises;
  clash = the big dynamic action, energy, motion;
  climax = the decisive instant (slow motion or extreme close-up);
  twist = a deadpan, funny anticlimax that recontextualises everything (or the TRIUMPH, when the
  episode asks for it: the win lands in front of a witness).
- camera: exactly ONE physical move per shot: "static locked-off", "slow push-in", "fast push-in",
  "pull-back reveal", "lateral tracking", "low tracking", "orbit", "crane up", "handheld follow",
  "whip pan", "slow-motion speed ramp" (climax only).
- action: the shot's movement written out beat by beat like a director's note, 30-60 words: who
  moves which body part, which way on screen, how fast, what it touches, and the exact pose it ends
  in. Refer to cast by name only. No dialogue, no thoughts, no text, no signs.
- framing: where everyone and every key prop sits in the frame as the shot starts, 15-35 words
  ("Juno lower-left third facing right, the wall outlet upper right, Mochi asleep bottom centre").
- performance: the acting: face and body, plus secondary motion (eyes snap wide, teeth gritted,
  hoodie strings swinging, hair whipping back, popcorn hanging in the air), 10-30 words.
- fx: the anime effect drawn in the picture this shot, if any (speed lines, a red power aura, an
  impact flash, a giant sweat drop), else "".
- WARDROBE IS LOCKED: nobody ever takes off, adds or swaps any clothing, at any point (gearing up
  is hands, eyes and stance, never clothes or hair). Never give anyone glasses, a hat, a bag or any
  accessory their cast description doesn't have. Props stay where the layout puts them.
- THE GAG READS ON SCREEN: a viewer with the sound off must see the cause, the move and the
  punchline. When the joke is a measure (too short, too late, too small), show the measure itself
  (the cable pulled taut, the phone stopping a hand's width short of her face).
- The examples in these rules come from other episodes: never copy their objects (popcorn, keys,
  a train, a hair tie) into this one. Every object in this episode comes from its own story.
- Every action must be literally drawable: a physical action with visible objects. Never abstract
  verbs (hack, outsmart, decide, realise, plan, figure out) and never a prop, gadget or power that
  wasn't visible earlier: anything that matters appears in the opening frame or an earlier shot.
- Every shot shows the main character or the one prop that matters. No empty shots of doors,
  trains or backgrounds.
- The story must read with the sound off; spoken lines only add flavour.
- sound: the one diegetic sound of the shot (footsteps on wet tiles, door seal hiss, cloth whoosh).
  Never speech, never music.
{dialogue}
- cast: names from the cast list in that shot (max 2), [] for an empty shot.
- location.plate_prompt describes the place EMPTY (no people, no animals), time of day, weather,
  light, blank unreadable signage, with the story's props only as start_state has them (never
  plugged in, switched on or in place if the story does that later). Posters and screens on the
  walls show plain abstract shapes, never the story's objects or symbols. light: same direction and
  colour temperature in every shot.
- hook (the Instagram caption, not a shot): line 1 is a one-line truth everyone recognises about this
  moment ("The doors always wait until you're one step away."), line 2 an optional curiosity line.
  No hashtags.
- music_prompt: genre, mood, instruments, "instrumental", builds to a hit on the climax. Never real
  artists or songs.
- Never use copyrighted characters, brands, logos, real people or real teams.

Respond with JSON only:
{{"title": str (1-2 words, max 14 chars), "category": str, "emoji": str, "logline": str, "hook": str,
  "location": {{"name": str, "plate_prompt": str}},
  "winning_move": str, "direction": str, "motif": str, "start_state": str, "opening_frame": str,
  "layout": str,
  "shots": [{{"beat": str, "seconds": int, "size": str, "angle": str, "camera": str, "cast": [str],
              "framing": str, "action": str, "performance": str, "fx": str,
              "line": str, "speaker": str, "ends": str, "sound": str, "light": str}}, ... exactly {n}],
  "music_prompt": str}}
The "shots" array must hold all {n} shots, every one complete."""


EPISODE_FIRST = (
    "\n\nTHIS ANSWER: every field EXCEPT the shots (title, category, emoji, logline, hook, "
    "location, winning_move, direction, motif, start_state, opening_frame, layout, music_prompt), "
    "each at its "
    'full length. Leave "shots" out: the shots are your next answer, written to match these.'
)


def episode_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Everything an answer said about the episode except its shots."""
    return {k: v for k, v in raw.items() if k != "shots" and k not in SHOT_KEYS}


def episode_errors(episode: dict[str, Any]) -> list[Any]:
    """What is wrong with an episode answer, its (absent) shots aside."""
    try:
        EpisodePlan.model_validate({**episode, "shots": []})
    except ValidationError as e:
        return [err for err in e.errors() if err["loc"] and err["loc"][0] != "shots"]
    return []


def has_shots(raw: object) -> bool:
    return isinstance(recover_shots(raw), dict) and isinstance(recover_shots(raw).get("shots"), list)


def shots_only_request(keep: dict[str, Any], n: int) -> str:
    return (
        "\n\nTHE EPISODE BELOW IS GOOD: keep every field of it exactly as written. Only the shots "
        "need writing again, fixing the problems above:\n"
        + json.dumps(keep, ensure_ascii=False)
        + f'\nRespond with JSON only: {{"shots": [...]}} holding exactly {n} shots, every field of '
        "every shot complete at its full length, following every storyboard rule."
    )


class Planner:
    def __init__(self, llm: JSONChat, series: Series, max_attempts: int = 6):
        self.llm = llm
        self.series = series
        self.max_attempts = max_attempts

    def system_prompt(self) -> str:
        s = self.series
        chars = "\n".join(f"- {c.name}: {c.description.strip()}" for c in s.characters) or "- (none)"
        if s.take.enabled:
            if s.take.dialogue == "bookends":
                talkers = [c.name for c in s.characters if c.speaks]
                mute = [c.name for c in s.characters if not c.speaks]
                dialogue = DIALOGUE_BOOKENDS.format(
                    max_lines=s.take.max_lines,
                    speakers=", ".join(talkers) or "(nobody)",
                    mute=f"{', '.join(mute)} never talk{'s' if len(mute) == 1 else ''} (sounds only)."
                    if mute
                    else "",
                )
            else:
                dialogue = DIALOGUE_NONE
            return SYSTEM_TAKE.format(
                dialogue=dialogue,
                seconds=s.take.seconds,
                n=s.take.shots,
                lo=s.take.min_shot_s,
                hi=s.take.max_shot_s,
                name=s.series.name,
                premise=s.series.premise,
                characters=chars,
                beats=" -> ".join(BEATS),
            )
        return SYSTEM.format(
            seconds=s.shots.count * s.shots.duration_s,
            n=s.shots.count,
            shot_s=s.shots.duration_s,
            name=s.series.name,
            premise=s.series.premise,
            characters=chars,
            beats=" -> ".join(BEATS[: s.shots.count]) if s.shots.count == len(BEATS) else "free",
        )

    def default_seed(self, episode_no: int) -> EpisodeSeed | None:
        eps = self.series.episodes
        return eps[episode_no - 1] if 0 < episode_no <= len(eps) else None

    def user_prompt(
        self,
        episode_no: int,
        past_titles: list[str],
        feedback: str = "",
        guidance: str = "",
        seed: EpisodeSeed | None | object = AUTO,
    ) -> str:
        s = self.series
        if seed is AUTO:
            seed = self.default_seed(episode_no)
        lines = [
            f"EPISODE {episode_no} of {s.series.total_episodes}.",
            f"SHOT_COUNT_JSON={json.dumps(s.shot_count)}",
        ]
        if seed:
            lines.append(f"Planned category: {seed.emoji} {seed.category}".strip())
            if seed.scripted:
                lines += [
                    "FIXED STORY: follow it exactly. Your job is shots, camera, timing, end states and "
                    "sound, not a new story. Keep every spoken line as written.",
                    *([f"- Location: {seed.location}"] if seed.location else []),
                    f"- Stake (hook/escalate): {seed.stake}",
                    f"- Winning move (climax): {seed.move}",
                    *([f"- Spectacle (clash/climax): {seed.spectacle}"] if seed.spectacle else []),
                    f"- Payoff (twist, the last image): {seed.payoff}",
                ]
            elif seed.idea:
                lines.append(f"Idea to build on: {seed.idea}")
        else:
            lines.append("No category planned: pick a fresh one that fits the premise.")
        film_rule = (
            "Borrow its camera language, framing and timing only. Never name the film in any field, "
            "never copy its characters, costumes, props, logos, lines or music."
        )
        if seed and seed.homage:
            lines.append(f"- Homage: stage it like {seed.homage}. {film_rule}")
        elif s.take.enabled and s.homages:
            lines.append(
                "- Homage: pick the ONE famous movie moment below that fits this gag best and stage the "
                f"climax like it. {film_rule} Options: " + "; ".join(s.homages)
            )
        if s.take.enabled and not (seed and seed.scripted):
            lines.append(ENDING_TRIUMPH if wins_this_episode(s, episode_no) else ENDING_DEADPAN)
        if guidance:  # lessons from earlier episodes (learn.planner_guidance)
            lines.append(guidance)
        if past_titles:
            lines.append("Already used (do not repeat titles or premises): " + "; ".join(past_titles))
        if feedback:
            lines.append(f"Your previous answer broke these rules: {feedback}. Fix them and answer again.")
        return "\n".join(lines)

    def critique(self, plan: EpisodePlan) -> tuple[int, str]:
        """A first-time viewer reads only what is on screen (shots + spoken lines) and says what
        happened. Returns (score 1-10, feedback for the director; empty when it's clear)."""
        shots = "\n".join(
            f"{i}. ({s.seconds}s, {s.size}) {s.action}" + (f' {s.speaker}: "{s.line}"' if s.line else "")
            for i, s in enumerate(plan.shots, 1)
        )
        user = (
            f"THE PLACE: {plan.location.plate_prompt}\n"
            + (f"AT THE START: {plan.start_state}\n" if plan.start_state else "")
            + f"OPENING FRAME: {plan.opening_frame}\nSHOTS:\n{shots}"
        )
        try:
            v = self.llm.json_chat(CRITIC_SYSTEM, user, temperature=0.2)
            score = int(v.get("score", 0))
        except Exception as e:  # the critic is a quality gate, never a single point of failure
            log.warning("story critic unavailable: %s", e)
            return 10, ""
        confusions = [str(c) for c in v.get("confusions") or []][:4]
        if v.get("clear") and score >= CRITIC_PASS and not confusions:
            return score, ""
        return score, (
            f'a first-time viewer understood it as: "{v.get("summary", "")}" (clarity {score}/10). '
            f"Confusing: {'; '.join(confusions) or 'the cause, the winning move or the punchline'}. "
            "Make cause -> winning move -> payoff literal and visible with the sound off"
        )

    def plan(
        self,
        episode_no: int,
        past_titles: list[str],
        guidance: str = "",
        seed: EpisodeSeed | None | object = AUTO,
    ) -> EpisodePlan:
        if seed is AUTO:
            seed = self.default_seed(episode_no)
        feedback = ""
        best: tuple[int, EpisodePlan] | None = None
        # An episode that came back good except for its shots (none, too few, broken) is kept, and
        # the next attempt asks for the shots alone: a smaller answer, every field at full length.
        keep: dict[str, Any] | None = None
        # The episode and its shots are two answers: one long answer got cut off on Groq (31 Oct).
        n = self.series.shot_count
        for attempt in range(1, self.max_attempts + 1):
            base = self.user_prompt(episode_no, past_titles, feedback, guidance, seed)
            temp = 0.9 if attempt == 1 else 0.7
            episode, keep = keep, None
            try:
                if episode is None:
                    raw = self.llm.json_chat(self.system_prompt(), base + EPISODE_FIRST, temperature=temp)
                    # a whole plan in one answer is taken as it is; so is junk (no title)
                    ok = isinstance(raw, dict) and raw.get("title") and not has_shots(raw)
                    episode = episode_fields(raw) if ok else None
                    if episode is not None and episode_errors(episode):
                        episode = None  # fix the episode first: its errors are fed back below
                if episode is not None:
                    shots = self.llm.json_chat(
                        self.system_prompt(), base + shots_only_request(episode, n), temperature=0.7
                    )
                    raw = {**episode, **(recover_shots(shots) if isinstance(shots, dict) else {})}
            except AnswerCutOff as e:
                keep = episode  # a good episode survives a cut-off shot list
                feedback = f"your last answer was cut off before the end ({e}); write it again, complete"
                log.warning("plan attempt %d cut off: %s", attempt, e)
                continue
            try:
                plan = EpisodePlan.model_validate(raw)
            except ValidationError as e:
                errors = e.errors()
                feedback = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in errors)
                keys = sorted(raw) if isinstance(raw, dict) else type(raw).__name__
                log.warning(
                    "plan attempt %d invalid: %s (answer had keys %s; starts %s)",
                    attempt,
                    feedback,
                    keys,
                    json.dumps(raw, ensure_ascii=False)[:600],
                )
                if any(err["loc"][:1] == ("shots",) and err["type"] == "missing" for err in errors):
                    feedback += (
                        '. Your JSON had no "shots" array: put every shot, complete, in "shots" at the '
                        "top level, next to title and location"
                    )
                if isinstance(raw, dict) and errors and all(err["loc"][:1] == ("shots",) for err in errors):
                    keep = episode_fields(raw)
                continue
            problems = storyboard_problems(self.series, plan)
            if problems:
                feedback = "; ".join(problems)
                log.warning("plan attempt %d breaks storyboard rules: %s", attempt, feedback)
                if isinstance(raw, dict):
                    keep = episode_fields(raw)
                continue
            plan = normalise(self.series, plan)
            if seed:  # the calendar wins over the model for category/emoji
                plan.category, plan.emoji = seed.category, seed.emoji or plan.emoji
            if not self.series.take.enabled:
                return plan
            score, feedback = self.critique(plan)
            if not feedback:
                return plan
            log.warning("plan attempt %d unclear to a viewer (%d/10): %s", attempt, score, feedback)
            if best is None or score > best[0]:
                best = (score, plan)
        if best:  # never block the day on the critic: ship the clearest valid storyboard
            log.warning("no storyboard passed the viewer check; using the clearest (%d/10)", best[0])
            return best[1]
        raise RuntimeError(f"planner failed after {self.max_attempts} attempts: {feedback}")
