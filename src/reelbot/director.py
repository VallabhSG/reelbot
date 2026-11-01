"""The director's pass: after the storyboard is planned, write the set, the first-frame blocking and
every shot out in full: every element in frame, the lens, the motion beat by beat, the acting, the
physics, the light, the effect, the last frame and the sound. Seedance only sees the opening frame;
after the first cut it draws everything from these words.

Modelled on how long-form production prompts for Seedance are written (Higgsfield's prompt library
and the ByteDance guide as summarised by its integrators, Oct 2026): a set described element by
element, blocking with screen positions and distances, a lens and ONE camera move per shot, the
action as ordered beats, acting as visible physical detail (never emotion words), secondary motion,
named light, and anime technique (held poses, smears, speed lines) only where the shot calls for it.
The planner writes the story; this pass never changes it, it only writes it out.

One call for the set and blocking, then one per shot: small calls stay inside free-tier token
limits, and each shot gets the model's full attention."""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from .models import EpisodePlan, ShotPlan
from .series import Series
from .storyboard import SIZE_TEXT, episode_cast

log = logging.getLogger(__name__)

SET_ROLE = "PRODUCTION DESIGNER"
SHOT_ROLE = "SHOT DIRECTOR"

SET_WORDS = (150, 220)
BLOCKING_WORDS = (50, 90)

_CLOTH = (
    r"(?:hoodie|hood|jacket|bomber|shirt|tee|t-shirt|top|tank|shoes|sneakers|high-tops|scarf|glasses"
    r"|hat|cap|coat|sweater|shorts|jeans|clothes|clothing|outfit)"
)
_OFF_VERB = r"(?:tak|pull|strip|peel|shrug|throw|toss|rip|tear|yank|whip|slip|kick|fling|flung|shed|lift)\w*"
# A shot that undresses or re-dresses anyone breaks the wardrobe lock (episode 8 lost Juno's hoodie).
CLOTHES_CHANGE = re.compile(
    "|".join(
        [
            rf"\b{_OFF_VERB}\b[^.;]{{0,30}}\b{_CLOTH}\b[^.;]{{0,20}}\b(?:off|away|over (?:her|his) head)\b",
            rf"\b{_OFF_VERB}\s+(?:off|out of)\b[^.;]{{0,20}}\b{_CLOTH}\b",
            rf"\b(?:remov|unzip|swap)\w*\b[^.;]{{0,12}}\b{_CLOTH}\b",
            r"\bchang\w* (?:into|her clothes|his clothes|clothes|outfits?)\b",
            rf"\b{_CLOTH}\b[^.;]{{0,15}}\b(?:comes?|came|fl(?:y|ies|ew)|falls?|fell|slides?|slid)\s+off\b",
        ]
    ),
    re.I,
)

SET_SYSTEM = f"""You are the {SET_ROLE} of a vertical 9:16 2D anime short. A video model will draw this
place from your words alone, so describe EVERY element the camera can see, fixed for the whole
episode. Plain concrete nouns, colours, materials, sizes and positions; present tense; no metaphors.

Write two things:

1. set ({SET_WORDS[0]}-{SET_WORDS[1]} words, one paragraph, back wall to foreground):
- the room or place itself: walls, floor, ceiling, windows, doors: material, colour, texture, wear
- every piece of furniture and every prop: where it is (which wall, left or right, how many metres
  from the others), its size, colour, material and state (on, off, open, plugged in, half-empty)
- the props the story uses, exactly: what they look like, how big or long, and where and how they
  START, exactly as the START STATE says (a charger the story plugs in later starts unplugged and
  out of reach); never already doing what the story makes them do later; they match the LAYOUT
- what is outside the windows or beyond the edges (time of day, weather, sky, lights)
- every light source (lamp, screen, window, neon) with its colour and which way it throws light and
  shadow
- atmosphere and small life: dust in a light beam, a plant, a mug, a crumpled blanket; posters,
  paintings and screens show only plain abstract shapes, never words and never the story's
  objects, motif or symbols
- RICH ENOUGH TO FILL EVERY FRAME: something in the foreground, the middle and the back from any
  angle; at least two light sources of different colours (a warm lamp and a cool window, a
  screen's glow); surfaces that catch light (a glossy floor, glass, a screen, metal, a wet sill)
  with their reflections; atmosphere in the light (dust, steam, haze); something alive in the
  background (a curtain moving, rain on the glass, a ceiling fan turning). Name materials and
  textures (worn wood grain, knitted wool, scuffed rubber, brushed steel)
- end with the palette: 3-5 named colours, one warm and one cool that fight each other
Never people or animals (the cast is described separately), never brands, logos or readable text,
nothing that contradicts the location description or the layout.

2. blocking ({BLOCKING_WORDS[0]}-{BLOCKING_WORDS[1]} words): the opening frame exactly: each character
and each key prop, its horizontal position as a share of the frame width ("x 30%"), how high in
the frame, which way it faces, its pose, and the distances between them in metres. It must match
the opening frame and shot 1 you are given.

Respond with JSON only: {{"set": str, "blocking": str}}"""

SHOT_SYSTEM = f"""You are the {SHOT_ROLE} and cinematographer of a vertical 9:16 2D anime short. A video
model that sees nothing but your words will draw this ONE shot, so write it out completely. The
story is fixed: keep the shot's action, cast, end state and line exactly as given; you only write
them out. Present tense, plain physical words, in this order:

1. One sentence: who does what (the shot's main action).
2. Lens and camera: focal length (14 mm ultra-wide, 24 mm wide, 35 mm, 50 mm, 85 mm portrait,
   100 mm macro), camera height (floor level, knee, waist, eye level, overhead), distance to the
   subject in metres, the ONE camera move with its speed and path, depth of field and what is in
   focus. Close shots get shallow focus with soft round bokeh from the lights behind; a rack focus
   says what is sharp first and what is sharp after. Quiet shots move slowly or not at all.
3. The frame as the shot starts, foreground, midground and background: every character and every
   element of the set that is visible, where it sits in the frame (left, centre or right third,
   top or bottom), its colour and material. Never a flat, empty background: name the foreground
   element, the subject and the background detail; a light source visible in frame when the shot
   allows; reflections and highlights on glossy surfaces.
4. The action as ordered beats ("first ... for half a second; then ...; in the last second ..."):
   which body part moves, which way on screen, how fast, what it touches, what it pushes or pulls.
5. The acting as visible detail: brows, eyes, pupils, mouth, breath, shoulders, hands. Never
   emotion words without the face that shows them.
6. Physics and secondary motion: hair, fabric folds and drawstrings, the cable, the cat's tail,
   dust, anything in the background that moves; weight, drag and follow-through, then settling.
7. Light on this shot: where the key light comes from, its colour, the rim light, any change
   during the shot (boss-mode red rim snapping on, a screen flaring).
8. Anime technique only where it fits: a held pose at the peak, a smear frame on the fastest move,
   speed lines from a vanishing point, an impact flash, an aura, a giant sweat drop, an anger vein,
   a chibi pop for the deadpan beat. Say its shape, colour, size, where in frame and when.
9. The last frame: the exact final pose and where everyone and every prop is.
10. Sound, in order: what is heard and when. Never music, never narration.

Rules:
- Characters wear exactly the look given, in every shot. Never write anyone taking off, adding,
  opening or changing any clothing, hair style or accessory; gearing up is hands, eyes and stance.
- Only things that exist in the set or that a character already carries. Nothing appears from
  nowhere; nothing moves unless a character or the camera moves it. Props are in the state the
  story has reached by this shot (unplugged until the shot that plugs it in).
- Keep the screen direction. Start exactly where the previous shot ended; end where the next shot
  starts.
- Refer to characters by name. No brands, no readable text, no logos.
- The word count you are given is a target: every sentence must add something the model can draw.

Respond with JSON only: {{"detail": str}}"""


class JSONChat(Protocol):
    def json_chat(self, system: str, user: str, temperature: float = 0.9) -> dict[str, Any]: ...


def shot_words(seconds: int) -> tuple[int, int]:
    """Words for a shot of this length: ~25-35 a second plus the fixed parts (lens, frame, last
    frame, sound). A 15 s reel in 6 shots: ~750-1,000 words of shots, ~1,500-1,800 in all."""
    return 25 * seconds + 60, 35 * seconds + 80


def _words(text: str) -> int:
    return len(text.split())


def _cap(text: str, max_words: int) -> str:
    """Cut an over-long answer at the last full sentence inside max_words."""
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    cut = " ".join(words[:max_words])
    end = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[: end + 1] if end > len(cut) // 2 else cut).strip()


def clothes_problem(text: str) -> str:
    m = CLOTHES_CHANGE.search(text)
    return f'"{m.group(0)}" changes someone\'s clothes' if m else ""


class Director:
    def __init__(self, llm: JSONChat, series: Series, temperature: float = 0.7):
        self.llm = llm
        self.s = series
        self.temperature = temperature

    # ------------------------------------------------------------------ context
    def _looks(self, names: list[str]) -> str:
        chars = {c.name: c for c in self.s.characters}
        return "\n".join(
            f"- {n}: {(chars[n].look or chars[n].description).strip().rstrip('.')}."
            for n in names
            if n in chars
        )

    def _episode(self, plan: EpisodePlan) -> str:
        cast = episode_cast(plan)
        lines = [
            f"EPISODE: {plan.title} ({plan.category}): {plan.logline.strip()}",
            f"STYLE: {self.s.style.bible.strip()}",
            f"LOCATION: {plan.location.name}: {plan.location.plate_prompt.strip()}",
        ]
        if plan.layout:
            lines.append(f"LAYOUT: {plan.layout.strip()}")
        if plan.start_state:
            lines.append(f"START STATE (before anything happens): {plan.start_state.strip()}")
        if plan.direction:
            lines.append(f"SCREEN DIRECTION: {plan.direction}")
        if plan.motif:
            lines.append(f"MOTIF: {plan.motif}")
        if plan.winning_move:
            lines.append(f"WINNING MOVE: {plan.winning_move}")
        lines.append(f"OPENING FRAME: {plan.opening_frame.strip()}")
        if cast:
            lines.append("CAST (exact looks, never changed):\n" + self._looks(cast))
        lines.append("SHOT LIST:\n" + "\n".join(self._shot_line(i, s) for i, s in enumerate(plan.shots, 1)))
        return "\n".join(lines)

    @staticmethod
    def _shot_line(i: int, s: ShotPlan) -> str:
        who = ", ".join(s.cast) or "nobody"
        return f"{i}. {s.seconds} s {s.beat.upper()}, {SIZE_TEXT[s.size].lower()}, {s.camera} ({who}): {s.action}"

    @staticmethod
    def _shot_brief(s: ShotPlan) -> str:
        parts = [
            f"beat: {s.beat}",
            f"length: {s.seconds} s",
            f"size: {SIZE_TEXT[s.size]}",
            f"angle: {s.angle}",
            f"camera move: {s.camera}",
            f"cast: {', '.join(s.cast) or 'nobody'}",
            f"framing: {s.framing}" if s.framing else "",
            f"action: {s.action}",
            f"acting: {s.performance}" if s.performance else "",
            f"effect: {s.fx}" if s.fx else "",
            f'spoken line: {s.speaker}: "{s.line}"' if s.line else "",
            f"ends on: {s.ends}" if s.ends else "",
            f"sound: {s.sound}" if s.sound else "",
            f"light: {s.light}",
        ]
        return "\n".join(p for p in parts if p)

    # ------------------------------------------------------------------ calls
    def _ask(self, system: str, user: str, key: str, lo: int, hi: int, check=None) -> str:
        """One answer, asked again once if it is too short, too long or breaks a rule."""
        note = ""
        text = ""
        for attempt in range(2):
            out = self.llm.json_chat(system, user + note, temperature=self.temperature)
            text = str(out.get(key) or "").strip()
            problem = check(text) if check else ""
            n = _words(text)
            if not problem and lo * 0.7 <= n <= hi * 1.4:
                return _cap(text, int(hi * 1.4))
            reason = problem or f"it was {n} words; write {lo}-{hi}"
            note = f"\n\nYOUR LAST ANSWER WAS REJECTED: {reason}. Write it again."
            log.info("director: %s rejected (attempt %d): %s", key, attempt + 1, reason)
        if check and check(text):
            return ""  # never ship a broken rule; the shot falls back to the storyboard fields
        return _cap(text, int(hi * 1.4)) if _words(text) >= lo // 2 else ""

    def write_set(self, plan: EpisodePlan) -> tuple[str, str]:
        user = (
            self._episode(plan)
            + f"\n\nWrite the set in {SET_WORDS[0]}-{SET_WORDS[1]} words and the blocking in "
            f"{BLOCKING_WORDS[0]}-{BLOCKING_WORDS[1]} words."
        )
        out = self.llm.json_chat(SET_SYSTEM, user, temperature=self.temperature)
        world = str(out.get("set") or "").strip()
        blocking = str(out.get("blocking") or "").strip()
        if _words(world) < SET_WORDS[0] * 0.7 or _words(blocking) < BLOCKING_WORDS[0] * 0.5:
            out = self.llm.json_chat(
                SET_SYSTEM,
                user + f"\n\nYOUR LAST ANSWER WAS TOO SHORT ({_words(world)} and {_words(blocking)} "
                "words). Write both again, in full.",
                temperature=self.temperature,
            )
            world = str(out.get("set") or world).strip()
            blocking = str(out.get("blocking") or blocking).strip()
        return _cap(world, int(SET_WORDS[1] * 1.4)), _cap(blocking, int(BLOCKING_WORDS[1] * 1.4))

    def write_shot(self, plan: EpisodePlan, i: int, world: str, blocking: str) -> str:
        s = plan.shots[i]
        lo, hi = shot_words(s.seconds)
        user = [self._episode(plan), f"SET: {world}" if world else ""]
        if i == 0 and blocking:
            user.append(f"OPENING FRAME BLOCKING (this shot starts on it): {blocking}")
        if i > 0:
            prev = plan.shots[i - 1]
            user.append(f"PREVIOUS SHOT ends on: {prev.ends or prev.action}")
        if i + 1 < len(plan.shots):
            user.append(f"NEXT SHOT starts with: {plan.shots[i + 1].action}")
        user.append(f"WRITE SHOT {i + 1} of {len(plan.shots)} in {lo}-{hi} words:\n{self._shot_brief(s)}")
        if s.cast:
            user.append("Looks in this shot, exactly:\n" + self._looks(s.cast))
        return self._ask(SHOT_SYSTEM, "\n\n".join(u for u in user if u), "detail", lo, hi, clothes_problem)

    def direct(self, plan: EpisodePlan) -> EpisodePlan:
        """The plan with world, blocking and every shot's detail written. A shot whose detail
        can't be written keeps detail="" and renders from the storyboard fields."""
        world, blocking = self.write_set(plan)  # fails loudly: the model is down, skip the pass
        shots = []
        for i, s in enumerate(plan.shots):
            try:
                detail = self.write_shot(plan, i, world, blocking)
            except Exception:  # one bad answer costs one shot's detail, not the episode's
                log.exception("director: shot %d failed", i + 1)
                detail = ""
            shots.append(s.model_copy(update={"detail": detail}))
        return plan.model_copy(update={"world": world, "blocking": blocking, "shots": shots})
