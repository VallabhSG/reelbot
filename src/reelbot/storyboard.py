"""Storyboard rules + prompt composition (v2: one prompt per shot; v3: one prompt for the whole take).

Recipe (from the reference reels and the Seedance/Kling prompting guides):
  * fixed beat order: hook -> escalate -> clash -> climax -> twist
  * shot-size variety: never the same size twice in a row, at least one close shot (CU/ECU)
    and one wide shot (WS/EWS) per reel
  * ONE location per episode, fed to every shot as a reference image, so scenes don't jump
  * every recurring character fed as a reference image in every shot they appear in
  * prompt formula: size + angle, camera move as a physical behaviour, ONE subject action with an
    end state, setting + light, style lock, negatives last
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import BEATS, EpisodePlan, ShotPlan
from .series import Series

SIZE_TEXT = {
    "EWS": "Extreme wide shot",
    "WS": "Wide shot",
    "MS": "Medium shot",
    "MCU": "Medium close-up",
    "CU": "Close-up",
    "ECU": "Extreme close-up",
}
CLOSE = {"CU", "ECU"}
WIDE = {"WS", "EWS"}


def canonical_cast(series: Series, names: list[str]) -> tuple[list[str], list[str]]:
    """Map names case-insensitively to series characters. Returns (known, unknown)."""
    lookup = {c.name.lower(): c.name for c in series.characters}
    known, unknown = [], []
    for n in names:
        hit = lookup.get(n.strip().lower())
        if hit and hit not in known:
            known.append(hit)
        elif not hit:
            unknown.append(n)
    return known, unknown


def default_beats(n: int) -> list[str]:
    """Beat sequence for n shots: every beat once, in order; extra shots go to clash first."""
    if n < len(BEATS):
        return list(BEATS[:n])
    extra = n - len(BEATS)
    more = {"clash": (extra + 1) // 2, "escalate": extra // 2}
    out: list[str] = []
    for b in BEATS:
        out += [b] * (1 + more.get(b, 0))
    return out


def fit_seconds(seconds: list[int], total: int, lo: int, hi: int) -> list[int]:
    """Scale shot lengths to sum to `total`, each within [lo, hi], keeping their proportions."""
    raw = [max(lo, min(hi, s)) for s in seconds] or []
    if not raw:
        return raw
    scaled = [s * total / sum(raw) for s in raw]
    out = [max(lo, min(hi, int(x))) for x in scaled]
    order = sorted(range(len(out)), key=lambda i: scaled[i] - int(scaled[i]), reverse=True)
    i = 0
    while sum(out) != total and i < 10 * len(out):
        j = order[i % len(out)]
        if sum(out) < total and out[j] < hi:
            out[j] += 1
        elif sum(out) > total and out[j] > lo:
            out[j] -= 1
        i += 1
    return out


def storyboard_problems(series: Series, plan: EpisodePlan) -> list[str]:
    """Rule violations, phrased as instructions the LLM can act on."""
    out: list[str] = []
    n = series.shot_count
    if len(plan.shots) != n:
        out.append(f"there must be exactly {n} shots, got {len(plan.shots)}")
    beats = tuple(s.beat for s in plan.shots)
    if n == len(BEATS) and beats != BEATS:
        out.append(f"beats must be exactly in this order: {', '.join(BEATS)} (got {', '.join(beats)})")
    elif n > len(BEATS):
        order = [BEATS.index(b) for b in beats]
        if order != sorted(order) or set(beats) != set(BEATS):
            out.append(
                f"use every beat at least once and never go backwards: {' -> '.join(BEATS)} "
                f"(got {', '.join(beats)})"
            )
    if series.take.enabled:
        if not plan.winning_move.strip():
            out.append("winning_move is required: the one decisive action the episode builds to")
        if not plan.opening_frame.strip():
            out.append("opening_frame is required: describe the very first frame")
        if not plan.motif.strip():
            out.append("motif is required: one small object or colour that recurs and pays off")
        if not plan.direction.strip():
            out.append("direction is required: the screen direction kept in every shot")
        for i, s in enumerate(plan.shots[:-1], 1):
            if not s.ends.strip():
                out.append(f"shot {i} needs 'ends': the exact visible state at the cut")
        out += dialogue_problems(series, plan)
    sizes = [s.size for s in plan.shots]
    for i in range(1, len(sizes)):
        if sizes[i] == sizes[i - 1]:
            out.append(f"shots {i} and {i + 1} are both {sizes[i]}; consecutive shots need different sizes")
    if not CLOSE & set(sizes):
        out.append("include at least one CU or ECU shot")
    if not WIDE & set(sizes):
        out.append("include at least one WS or EWS shot")
    for i, s in enumerate(plan.shots, 1):
        _, unknown = canonical_cast(series, s.cast)
        if unknown:
            out.append(
                f"shot {i} cast {unknown} unknown; use only {[c.name for c in series.characters]} or []"
            )
    if series.characters and not any(s.cast for s in plan.shots):
        out.append("the recurring characters must appear in the episode")
    return out


SPEAKING_BEATS = {"hook", "escalate", "twist"}  # dialogue bookends: set-up and punchline only
MAX_LINE_WORDS = 10


def dialogue_problems(series: Series, plan: EpisodePlan) -> list[str]:
    out: list[str] = []
    spoken = [(i, s) for i, s in enumerate(plan.shots, 1) if s.line.strip()]
    if series.take.dialogue == "none":
        if spoken:
            out.append("this series is wordless: leave every 'line' empty")
        return out
    speakers = {c.name for c in series.characters if c.speaks}
    if len(spoken) > series.take.max_lines:
        out.append(f"at most {series.take.max_lines} spoken lines per episode, got {len(spoken)}")
    for i, s in spoken:
        if s.beat not in SPEAKING_BEATS:
            out.append(f"shot {i} is '{s.beat}': lines only in hook/escalate (set-up) or twist (punchline)")
        if len(s.line.split()) > MAX_LINE_WORDS:
            out.append(f"shot {i} line is too long: max {MAX_LINE_WORDS} words")
        who, _ = canonical_cast(series, [s.speaker]) if s.speaker else ([], [])
        if not who or who[0] not in speakers or who[0] not in s.cast:
            out.append(
                f"shot {i} speaker must be a character in that shot who can talk ({', '.join(sorted(speakers))})"
            )
    return out


def normalise(series: Series, plan: EpisodePlan) -> EpisodePlan:
    for s in plan.shots:
        s.cast, _ = canonical_cast(series, s.cast)
        if s.speaker:
            who, _ = canonical_cast(series, [s.speaker])
            s.speaker = who[0] if who else s.speaker
        if not s.line.strip():
            s.line, s.speaker = "", ""
    if series.take.enabled:
        t = series.take
        for s, sec in zip(
            plan.shots,
            fit_seconds([s.seconds for s in plan.shots], t.seconds, t.min_shot_s, t.max_shot_s),
            strict=True,
        ):
            s.seconds = sec
    return plan


def episode_cast(plan: EpisodePlan) -> list[str]:
    seen: list[str] = []
    for s in plan.shots:
        seen += [n for n in s.cast if n not in seen]
    return seen


def shot_starts(plan: EpisodePlan) -> list[int]:
    starts, t = [], 0
    for s in plan.shots:
        starts.append(t)
        t += s.seconds
    return starts


@dataclass
class TakePrompt:
    prompt: str  # the whole reel: sections + timecoded shot list
    global_prompt: str  # everything except the shot list (for models with a native shot-list field)
    shots: list[dict[str, object]]  # [{prompt, duration}] per shot, for that shot-list field
    roles: list[str]  # reference images in order: "opening" | "location" | character names


def compose_take_prompt(
    series: Series, plan: EpisodePlan, token: str, start_frame: bool, max_chars: int = 0
) -> TakePrompt:
    """v3: one prompt for the whole reel, cuts inside the generation.

    Built like the long production prompts written for Seedance (Higgsfield's library, ByteDance's
    guide): reference mapping and a one-line summary, the style and animation technique, a "keep"
    block per character, the location, then (from the director's pass) the set element by element
    and the first-frame blocking, the layout, the numbered shot list ("Shot 2 (3 s, CLASH): Hard
    cut."), and physics, light, audio and positive locks to close. Shots are numbered rather than
    timecoded: Seedance 2.0 paces numbered shots more reliably than exact timestamps.
    start_frame=True: the model starts from the composed opening frame (no other references).
    Otherwise image 1 is the location plate and the character sheets follow."""
    chars = {c.name: c for c in series.characters}
    cast = episode_cast(plan)
    roles = ["opening", *cast] if start_frame else ["location", *cast]
    ref = {r: token.format(n=i + 1) for i, r in enumerate(roles)} if token else {}
    total = sum(s.seconds for s in plan.shots)
    logline = plan.logline.strip().rstrip(".")
    sep = "━━━━━━━━"

    head: list[str] = []
    if ref:
        mapping = [
            f"{ref[r]} as the "
            + (
                "first frame"
                if r == "opening"
                else "location reference"
                if r == "location"
                else f"{r} reference"
            )
            for r in roles
        ]
        head.append("Use " + ", ".join(mapping) + ".")
    if start_frame:
        head.append("Shot 1 starts exactly on the given first frame.")
    head += [
        f"Generate a {total}-second vertical 9:16 cinematic anime short in {len(plan.shots)} shots with "
        f"hard cuts, one continuous scene: {logline}.",
        "STYLE, strictly consistent in every shot: " + series.style.bible.strip().rstrip(".") + ".",
    ]
    if series.style.animation:
        head.append("ANIMATION: " + series.style.animation.strip().rstrip(".") + ".")
    if cast:
        head.append(
            "Characters must match their references exactly: never change faces, hair, outfits, body "
            "proportions or props; fully part of the scene, never outlined, glowing or cut out. "
            "No extra main characters."
        )
    blocks = [sep]
    for n in cast:
        c = chars[n]
        tag = f" (reference {ref[n]})" if n in ref else ""
        blocks.append(f"[CHARACTER: {n}]{tag} Keep: {c.description.strip().rstrip('.')}.")
    loc_ref = f" (reference {ref['location']})" if "location" in ref else ""
    loc = f"[LOCATION]{loc_ref} "
    loc += plan.location.plate_prompt.strip().rstrip(".") + "."
    if plan.direction:
        loc += f" Screen direction: {plan.direction}, kept in every shot."
    if plan.motif:
        loc += f" Recurring detail: {plan.motif}."
    blocks.append(loc)
    if plan.world:
        blocks.append(f"[SET, every element fixed in every shot] {plan.world.strip()}")
    if plan.layout:
        blocks.append(
            f"[LAYOUT, fixed in every shot] {plan.layout.strip().rstrip('.')}. Nothing moves unless a "
            "character picks it up on screen."
        )
    if plan.blocking:
        blocks.append(f"[FIRST FRAME AND BLOCKING] {plan.blocking.strip()}")
    blocks.append(sep)

    spoken = any(s.line for s in plan.shots)
    audio = "[AUDIO] Synced sound effects for every action"
    audio += (
        "; the quoted lines are spoken by the named character, lip-synced, short and clear. "
        "No other speech, no narration, no music, no singing."
        if spoken
        else ". No speech, no dialogue, no narration, no music, no singing."
    )
    locks = ""
    if cast:
        looks = "; ".join(f"{n}: {(chars[n].look or chars[n].short or n).strip().rstrip('.')}" for n in cast)
        locks = (
            f"[LOCKS] Wardrobe and look, identical in every shot: {looks}. Nobody takes off, adds or "
            "changes any clothing, hair style or accessory, ever. "
            f"Cast: only {', '.join(cast)}, no one else. "
            + (f"Screen direction: {plan.direction}, in every shot. " if plan.direction else "")
            + "Every prop stays where the set and layout put it unless a character moves it on screen; "
            "each shot starts exactly where the previous one ended. Faces and hands stay stable and "
            "well drawn."
        )
    negative = " ".join(
        x
        for x in [
            series.style.negative.strip(),
            "" if "subtitle" in series.style.negative.lower() else "No subtitles, no on-screen text.",
        ]
        if x
    )
    tail = [
        sep,
        "[PHYSICS] Hair, fabric, drawstrings, cables and tails move with weight, drag and "
        "follow-through, then settle; every contact lands with impact; nothing floats, melts or morphs.",
        f"[LIGHTING] {plan.shots[0].light.strip().rstrip('.')}, the same in every shot."
        if plan.shots
        else "",
        audio,
        locks,
        f"[NEGATIVE] {negative}",
        f"Final effect: a tightly paced {total}-second anime short with clean hard cuts: {logline}. "
        + (
            f"Everything builds to one decisive move: {plan.winning_move.strip().rstrip('.')}. "
            if plan.winning_move
            else ""
        )
        + "Slow to fast, a burst on that move, one quiet beat, then the ending.",
    ]

    def who(name: str, look: str) -> str:
        """The name plus the look, in every shot: the model only sees the first frame, so after a
        cut it redraws everyone from these words. look: "full", "short" or "" (name only)."""
        c = chars.get(name)
        text = ((c.look if look == "full" else "") or c.short) if c and look else ""
        text = text.strip().rstrip(".")
        return f"{name} ({text}, unchanged)" if text else name

    def shot_list(
        detail: float, sound: bool, ends: bool, acting: bool = True, look: str = "full"
    ) -> tuple[list[str], list[dict[str, object]]]:
        """detail: the share of the director's text to keep (0 = use the storyboard fields)."""
        lines, shots, light = [], [], plan.shots[0].light if plan.shots else ""
        for i, s in enumerate(plan.shots, 1):
            subj = ", ".join(who(n, look) for n in s.cast)
            body = f"{SIZE_TEXT[s.size]}, {s.angle}, {s.camera}. "
            if detail and s.detail:
                # the director's pass wrote the whole shot: lens, frame, beats, acting, physics,
                # light, effect, last frame and sound
                body += (f"In frame: {subj}. " if subj else "") + trim_words(s.detail, detail)
                if s.line:
                    body += f' {s.speaker} says: "{s.line.strip()}"'
            else:
                act = s.action.strip().rstrip(".")
                if s.framing:
                    body += f"Framing: {s.framing.strip().rstrip('.')}. "
                body += f"{subj}: {act}." if subj else f"{act}."
                if acting and s.performance:
                    body += f" Acting: {s.performance.strip().rstrip('.')}."
                if acting and s.fx:
                    body += f" Effect: {s.fx.strip().rstrip('.')}."
                if s.line:
                    body += f' {s.speaker} says: "{s.line.strip()}"'
                if ends and s.ends and i < len(plan.shots):
                    body += f" Ends on: {s.ends.strip().rstrip('.')}."
                if s.light != light:
                    body += f" Light: {s.light.strip().rstrip('.')}."
                    light = s.light
                if sound and s.sound:
                    body += f" Sound: {s.sound.strip().rstrip('.')}."
            cut = "" if i == 1 else "Hard cut. "
            lines.append(f"Shot {i} ({s.seconds} s, {s.beat.upper()}): {cut}{body}")
            shots.append({"prompt": body[:500], "duration": s.seconds})
        return lines, shots

    global_prompt = "\n".join([" ".join(head), *blocks, *[t for t in tail if t]])
    # Over the model's limit: first shorten the director's shots from the end (sound, then the last
    # frame, light and physics go first; the action and the lens lead every shot) and the looks,
    # then fall back to the storyboard fields and drop the least important detail (per-shot
    # sounds, then acting and effects, then end states, then the looks; camera, framing and action
    # are never cut), then trim the global sections.
    levels = (
        (1.0, True, True, True, "full"),
        (0.85, True, True, True, "full"),
        (0.7, True, True, True, "full"),
        (0.7, True, True, True, "short"),
        (0.55, True, True, True, "short"),
        (0, True, True, True, "full"),
        (0, False, True, True, "full"),
        (0, False, True, False, "full"),
        (0, False, False, False, "full"),
        (0, False, False, False, "short"),
        (0, False, False, False, ""),
    )
    if not any(s.detail for s in plan.shots):
        levels = levels[5:]
    for detail, sound, ends, acting, look in levels:
        lines, shots = shot_list(detail, sound, ends, acting, look)
        prompt = "\n".join([" ".join(head), *blocks, *lines, *[t for t in tail if t]])
        if not max_chars or len(prompt) <= max_chars:
            break
    if max_chars and len(prompt) > max_chars:
        # still too long: keep the mapping + premise line and the full shot list, then fill what
        # room is left with style, characters and the closing sections
        n_core = 1 + bool(ref) + bool(start_frame)
        core = "\n".join([" ".join(head[:n_core]), *lines])[:max_chars]
        rest = "\n".join([" ".join(head[n_core:]), *blocks, *[t for t in tail if t]])
        room = max_chars - len(core) - 1
        prompt = core + ("\n" + rest[:room] if room > 20 else "")
    if max_chars and len(global_prompt) > max_chars:
        global_prompt = global_prompt[:max_chars]
    return TakePrompt(prompt=prompt, global_prompt=global_prompt, shots=shots, roles=roles)


def compose_keyframe_prompt(series: Series, plan: EpisodePlan, cast: list[str]) -> str:
    """The opening frame, painted with the characters already inside the location."""
    chars = {c.name: c for c in series.characters}
    refs = [f"image {i + 1} is {n} ({chars[n].short or n})" for i, n in enumerate(cast)]
    refs.append(f"image {len(cast) + 1} is the location")
    first = plan.shots[0]
    return " ".join(
        x
        for x in [
            f"Opening frame of a vertical anime film, {SIZE_TEXT[first.size].lower()}, {first.angle}:",
            plan.opening_frame.strip().rstrip(".") + ".",
            f"Reference images: {'; '.join(refs)}.",
            f"Setting: the {plan.location.name}, light: {first.light}.",
            f"The set: {clip_text(plan.world, 900)}" if plan.world else "",
            f"Blocking: {clip_text(plan.blocking, 500)}" if plan.blocking else "",
            "Draw the characters inside the scene: same light and colour grade, cast shadows, correct "
            "scale and perspective. No outlines, no glow, no white background, no cut-out look, "
            "no character-sheet layout, one pose each.",
            "Faces, hair, outfits and colours exactly as in their reference images.",
            series.style.bible.strip().rstrip(".") + ".",
            series.style.negative.strip(),
        ]
        if x
    ).strip()


def trim_words(text: str, share: float) -> str:
    """Keep about `share` of the words, cut at a sentence end (1.0 = all of it)."""
    text = text.strip()
    words = text.split()
    if share >= 1 or not words:
        return text
    cut = " ".join(words[: max(1, int(len(words) * share))])
    end = cut.rfind(". ")
    return cut[: end + 1] if end > len(cut) // 3 else cut.rstrip(",;") + "."


def clip_text(text: str, n: int) -> str:
    """At most n characters, cut at a sentence end (image prompts are shorter than video ones)."""
    text = text.strip()
    if len(text) <= n:
        return text
    cut = text[:n]
    end = cut.rfind(". ")
    return cut[: end + 1] if end > n // 2 else cut.rstrip() + "."


def compose_shot_prompt(
    series: Series, plan: EpisodePlan, shot: ShotPlan, token: str
) -> tuple[str, list[str]]:
    """Returns (prompt, ordered reference roles). Roles are character names plus 'location' last;
    the caller turns them into image URLs in the same order, so @image_N lines up."""
    roles = [*shot.cast, "location"]
    tag = {role: token.format(n=i + 1) for i, role in enumerate(roles)}
    chars = {c.name: c for c in series.characters}

    subjects = ", ".join(
        f"{name} ({tag[name]}, {chars[name].short or chars[name].description})" for name in shot.cast
    )
    parts = [
        f"{SIZE_TEXT[shot.size]}, {shot.angle}, {shot.camera}.",
        f"{subjects}: {shot.action}." if subjects else f"{shot.action}.",
        f"Setting: the {plan.location.name} shown in {tag['location']}, {shot.light}.",
        series.style.bible.strip().rstrip(".") + ".",
    ]
    if shot.cast:
        parts.append(
            "Keep every character's face, hair, outfit and colours exactly as in their reference image."
        )
    if series.style.negative:
        parts.append(series.style.negative.strip())
    prompt = " ".join(p for p in parts if p)
    return prompt, roles
