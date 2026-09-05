#!/usr/bin/env python3
"""
suno_video_infra.py — LLM Governance Toolkit
Video infra for Suno prompts.

    suno_prompts/<name>_suno.txt ─▶ storyboard ─▶ Runway clips ─▶ ffmpeg assembly
                                       │                              ▲
                                       └── (no key / no clips) ───────┘  local visualizer fallback

One shot per bracketed section of the Suno prompt. Repeated sections (Chorus,
Station ident, …) share one shot so a 12-section song costs ~7 generations.

Layout (all under the repo root):

    suno_prompts/<name>_suno.txt          the prompt (title / [Style:] / [Section - cue] / lyrics)
    suno_video/<name>/
        storyboard.json                   shot list — timing, image prompt, motion prompt
        storyboard.md                     same, human-readable, paste-ready for runway.com
        audio.mp3|wav|m4a                 DROP THE SUNO DOWNLOAD HERE (or pass --audio)
        timings.json                      optional: {"Verse 1": 12.4, "Chorus": 41.0, ...} start seconds
        frames/NN_<slug>.png              Runway text_to_image keyframes
        clips/NN_<slug>.mp4               Runway image_to_video clips (or your own — same names)
        <name>.mp4                        final video
        manifest.json                     what was used to build the final video

Commands:

    python tools/suno_video_infra.py storyboard all              # parse every prompt → storyboards
    python tools/suno_video_infra.py storyboard agi_radio_001_sign_on
    python tools/suno_video_infra.py generate  agi_radio_001_sign_on   # Runway API (needs RUNWAYML_API_SECRET)
    python tools/suno_video_infra.py assemble  agi_radio_001_sign_on --audio ~/Downloads/track.mp3
    python tools/suno_video_infra.py run       agi_radio_001_sign_on --audio ~/Downloads/track.mp3
    python tools/suno_video_infra.py status
    python tools/suno_video_infra.py runway check                   # proves the key in .env works (GET /organization)

Runway (https://docs.dev.runwayml.com):
    key     RUNWAYML_API_SECRET — read from the environment or the repo .env (gitignored)
    base    https://api.dev.runwayml.com/v1
    headers Authorization: Bearer $RUNWAYML_API_SECRET   X-Runway-Version: 2024-11-06
    POST /text_to_image   {model: gen4_image, promptText, ratio, referenceImages?}
    POST /image_to_video  {model: gen4.5 | gen4_turbo, promptImage, promptText, ratio, duration: 5|10}
    GET  /tasks/{id}      {status: PENDING|RUNNING|SUCCEEDED|FAILED|CANCELLED, output: [url]}

Fail-closed: no audio → no final video (a storyboard is not a video).
             no key   → generate refuses, assemble falls back to the visualizer and says so.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

TOOLKIT_VERSION = 37
RUNWAY_BASE = "https://api.dev.runwayml.com/v1"
RUNWAY_VERSION = "2024-11-06"
IMAGE_MODEL = os.environ.get("SUNO_VIDEO_IMAGE_MODEL", "gen4_image")
VIDEO_MODEL = os.environ.get("SUNO_VIDEO_VIDEO_MODEL", "gen4.5")
VIDEO_RATIO = "1280:720"     # gen4 video ratios: 1280:720 720:1280 1104:832 832:1104 960:960 1584:672
IMAGE_RATIO = "1920:1080"    # gen4_image ratios: 1920:1080 1080:1920 1024:1024 1168:880 ...
WIDTH, HEIGHT, FPS = 1280, 720, 24

ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = ROOT / "suno_prompts"
VIDEO_DIR = ROOT / "suno_video"


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines from the repo .env into os.environ (existing env wins). No dependency."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv()


# ──────────────────────────────────────────────────────────────────────────────
# 1. PARSE
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Section:
    index: int
    name: str            # "Verse 1", "Chorus", "Station ident"
    cue: str             # text after the dash inside the bracket
    lines: list[str]     # lyric lines (performance notes in parentheses stripped)
    notes: list[str]     # (performance notes)
    kind: str = ""       # intro / verse / chorus / bridge / outro / ident / other

    @property
    def key(self) -> str:
        """Sections with the same key share a shot."""
        return re.sub(r"\s+", " ", self.name.strip().lower())


@dataclass
class SunoPrompt:
    name: str
    title: str
    style: str
    sections: list[Section]
    source: str


SECTION_RE = re.compile(r"^\[(?!Style:)([^\]]+)\]\s*$", re.I)
STYLE_RE = re.compile(r"^\[Style:\s*(.+?)\]\s*$", re.I | re.S)
KIND_WORDS = {
    "intro": "intro", "verse": "verse", "pre-chorus": "prechorus", "prechorus": "prechorus",
    "chorus": "chorus", "hook": "chorus", "bridge": "bridge", "outro": "outro",
    "ident": "ident", "interlude": "interlude", "break": "interlude", "drop": "chorus",
    "solo": "interlude", "coda": "outro", "refrain": "chorus",
}


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return s or "x"


def parse_prompt(path: Path) -> SunoPrompt:
    raw = path.read_text(encoding="utf-8")
    lines = [l.rstrip() for l in raw.splitlines()]
    title = next((l.strip() for l in lines if l.strip()), path.stem)
    style = ""
    m = STYLE_RE.search(raw.replace("\n", " ")) if "[Style:" in raw else None
    if m:
        style = re.sub(r"\s+", " ", m.group(1)).strip()
    else:
        # multi-line style block
        buf, inside = [], False
        for l in lines:
            if l.strip().lower().startswith("[style:"):
                inside = True
                buf.append(l.strip()[7:])
            elif inside:
                buf.append(l.strip())
            if inside and l.strip().endswith("]"):
                break
        style = re.sub(r"\s+", " ", " ".join(buf)).strip().rstrip("]").strip()

    sections: list[Section] = []
    cur: Optional[Section] = None
    for l in lines:
        s = l.strip()
        if not s or s == title or s.lower().startswith("[style:"):
            continue
        sm = SECTION_RE.match(s)
        if sm:
            head = sm.group(1)
            name, _, cue = head.partition(" - ")
            if not cue:
                name, _, cue = head.partition(" – ")
            if not cue:
                name, _, cue = head.partition(":")
            name = name.strip()
            kind = next((v for k, v in KIND_WORDS.items() if k in name.lower()), "other")
            cur = Section(len(sections), name, cue.strip(), [], [], kind)
            sections.append(cur)
            continue
        if cur is None:
            cur = Section(0, "Intro", "", [], [], "intro")
            sections.append(cur)
        if s.startswith("(") and s.endswith(")"):
            cur.notes.append(s.strip("()"))
        else:
            cur.lines.append(re.sub(r"\s*\([^)]*\)", "", s).strip())
    name = re.sub(r"_suno$", "", path.stem)
    return SunoPrompt(name, title, style, sections, str(path.relative_to(ROOT)))


# ──────────────────────────────────────────────────────────────────────────────
# 2. STORYBOARD — style → visual language, section → shot
# ──────────────────────────────────────────────────────────────────────────────

# style keyword → visual vocabulary (what the camera sees, not what the ear hears)
VISUAL_MAP: list[tuple[str, str]] = [
    ("pirate radio",   "a cramped late-night radio booth, glowing VU meters, shortwave dials, one desk lamp"),
    ("radio",          "vintage broadcast equipment, tuning dials, red ON AIR lamp"),
    ("shortwave",      "shortwave static rendered as drifting light bands across a dark room"),
    ("spoken word",    "a single person alone at a microphone, cinematic close-ups, long shadows"),
    ("downtempo",      "slow drifting camera, haze, neon reflections on wet surfaces"),
    ("electronic",     "clean geometric light, LED grids, glass and chrome, cool blues and magentas"),
    ("techno",         "concrete tunnel, strobing white light, industrial machinery in motion"),
    ("synthwave",      "retro-futurist grid horizon, sunset gradients, chrome typography"),
    ("orchestral",     "a vast stone hall, string section in silhouette, dust in shafts of daylight"),
    ("cinematic",      "anamorphic lens flares, wide establishing shots, epic scale"),
    ("choir",          "a cathedral nave, candlelight, robed figures seen from behind"),
    ("cello",          "close-up of bow on strings, warm amber light"),
    ("strings",        "slow-motion bows, resin dust, golden hour"),
    ("metal",          "iron foundry, sparks, black steel, stage strobes, aggressive handheld camera"),
    ("anthem",         "crowd from above, raised hands, banners, stadium light"),
    ("hip hop",        "night city streets, sodium lamps, low angle tracking shots"),
    ("trap",           "high-contrast night, chrome, slow motion smoke"),
    ("ambient",        "empty landscapes, fog, one moving element, very slow zoom"),
    ("piano",          "a grand piano in an empty room, window light, dust"),
    ("folk",           "wooden interiors, fields at dusk, handheld warmth"),
    ("hungarian",      "Budapest at night, Danube bridges, tram lights, Central European grit"),
    ("budapest",       "Budapest rooftops and courtyards, the Chain Bridge, yellow trams"),
    ("hopeful",        "dawn light breaking through, warm palette"),
    ("solemn",         "muted palette, stillness, single subject"),
    ("ceremonial",     "processions, torches, an empty throne, hands passing an object"),
    ("dark",           "near-black frames, one light source"),
    ("minor key",      "cold desaturated tones, rain"),
    ("vocoder",        "digital artefacts, glitch, waveform overlays"),
    ("tape hiss",      "16mm film grain, light leaks"),
    ("warm",           "tungsten light, amber, soft focus edges"),
    ("cold",           "steel blue, fluorescent, hard edges"),
]

KIND_MOTION = {
    "intro":      "slow push-in, almost static, letting the scene breathe",
    "verse":      "steady slow tracking shot, intimate, following one subject",
    "prechorus":  "camera lifts slightly, light begins to change, tension builds",
    "chorus":     "wide shot, full energy, sweeping camera move, the biggest image in the film",
    "bridge":     "stillness, single held frame with one subtle motion",
    "interlude":  "abstract macro detail, textures, slow rotation",
    "ident":      "hard cut to a graphic, flat, dry, motionless except a flicker",
    "outro":      "slow pull-out and fade, the scene emptying",
    "other":      "slow cinematic camera move",
}

# relative duration weights when no audio timings are given
KIND_WEIGHT = {"intro": 0.6, "verse": 1.2, "prechorus": 0.6, "chorus": 1.0, "bridge": 0.8,
               "interlude": 0.6, "ident": 0.25, "outro": 0.7, "other": 0.8}


def visual_language(style: str) -> list[str]:
    s = style.lower()
    out = [v for k, v in VISUAL_MAP if k in s]
    if not out:
        out = ["cinematic, moody, one strong light source, shallow depth of field"]
    return out[:5]


@dataclass
class Shot:
    index: int
    section: str
    key: str
    kind: str
    cue: str
    lyric_hint: str
    start: float
    duration: float
    image_prompt: str
    motion_prompt: str
    clip_seconds: int          # 5 or 10 — Runway clip length; looped/trimmed to duration
    frame: str = ""            # frames/NN_slug.png (relative to song dir)
    clip: str = ""             # clips/NN_slug.mp4
    shared_with: list[int] = field(default_factory=list)


def build_storyboard(p: SunoPrompt, audio_seconds: Optional[float], timings: Optional[dict]) -> dict:
    vis = visual_language(p.style)
    palette = ", ".join(vis)
    # --- timing
    n = len(p.sections)
    if timings:
        starts = []
        for s in p.sections:
            starts.append(float(timings.get(s.name, timings.get(str(s.index), -1))))
        if any(x < 0 for x in starts):
            raise SystemExit(f"timings.json must give a start for every section: {[s.name for s in p.sections]}")
        total = audio_seconds or (starts[-1] + 20)
        durs = [(starts[i + 1] if i + 1 < n else total) - starts[i] for i in range(n)]
    else:
        weights = [KIND_WEIGHT.get(s.kind, 0.8) * max(1, len(s.lines) or 2) for s in p.sections]
        total = audio_seconds or sum(weights) * 3.0
        durs = [total * w / sum(weights) for w in weights]
        starts = [sum(durs[:i]) for i in range(n)]

    # --- shots (shared by key)
    first_by_key: dict[str, int] = {}
    shots: list[Shot] = []
    for s, st, du in zip(p.sections, starts, durs):
        lyric = " / ".join(l for l in s.lines[:3] if l)
        subject = s.cue or s.name
        image_prompt = (
            f"{palette}. Scene: {subject}. "
            f"Mood of the lyric: \"{lyric[:140]}\". "
            f"Film still, 16:9, photoreal, no text, no captions, no watermark, consistent with the other frames of the same film."
        ).replace("  ", " ")
        motion = KIND_MOTION.get(s.kind, KIND_MOTION["other"])
        if s.cue:
            motion = f"{motion}; {s.cue}"
        clip_seconds = 10 if du > 7 else 5
        sh = Shot(s.index, s.name, s.key, s.kind, s.cue, lyric, round(st, 2), round(du, 2),
                  image_prompt, motion, clip_seconds)
        slug = f"{s.index:02d}_{slugify(s.name)}"
        if s.key in first_by_key:
            master = shots[first_by_key[s.key]]
            master.shared_with.append(s.index)
            sh.frame, sh.clip = master.frame, master.clip
        else:
            first_by_key[s.key] = len(shots)
            sh.frame, sh.clip = f"frames/{slug}.png", f"clips/{slug}.mp4"
        shots.append(sh)

    unique = len(first_by_key)
    return {
        "name": p.name, "title": p.title, "source": p.source, "style": p.style,
        "visual_language": vis, "ratio": VIDEO_RATIO, "image_model": IMAGE_MODEL, "video_model": VIDEO_MODEL,
        "audio_seconds": audio_seconds, "timing_source": "timings.json" if timings else ("audio+weights" if audio_seconds else "weights-only (no audio yet)"),
        "unique_shots": unique, "sections": n,
        "anchor_prompt": f"{palette}. Establishing image that sets the palette and world of the film \"{p.title}\". Film still, 16:9, photoreal, no text.",
        "shots": [asdict(s) for s in shots],
        "toolkit_version": TOOLKIT_VERSION,
    }


def storyboard_md(sb: dict) -> str:
    out = [f"# {sb['title']} — storyboard", "",
           f"Source: `{sb['source']}`  ·  {sb['sections']} sections → {sb['unique_shots']} unique shots  ·  timing: {sb['timing_source']}", "",
           f"**Style:** {sb['style']}", "",
           "**Visual language:** " + "; ".join(sb["visual_language"]), "",
           "## Anchor frame (generate first, reuse as @anchor reference)", "", "```", sb["anchor_prompt"], "```", "",
           "## Shots", ""]
    for s in sb["shots"]:
        shared = f" (reuses shot {s['key']!r})" if not s["clip"].endswith(f"{s['index']:02d}_{slugify(s['section'])}.mp4") else ""
        out += [f"### {s['index']:02d} · {s['section']}  `{s['start']:.1f}s → +{s['duration']:.1f}s`{shared}", ""]
        if s["cue"]:
            out += [f"*Cue:* {s['cue']}", ""]
        if s["lyric_hint"]:
            out += [f"*Lyric:* {s['lyric_hint']}", ""]
        if not shared:
            out += ["Image prompt (text_to_image):", "```", s["image_prompt"], "```",
                    f"Motion prompt (image_to_video, {s['clip_seconds']}s):", "```", s["motion_prompt"], "```", ""]
    out += ["---", "Drop the Suno download as `audio.mp3` next to this file, put clips in `clips/` with the names above, then:",
            "", "```", f"python tools/suno_video_infra.py assemble {sb['name']}", "```"]
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────────────
# 3. RUNWAY
# ──────────────────────────────────────────────────────────────────────────────

class Runway:
    def __init__(self):
        key = os.environ.get("RUNWAYML_API_SECRET")
        if not key:
            raise SystemExit("RUNWAYML_API_SECRET not set — generate refuses (fail-closed). "
                             "Get a key at https://dev.runwayml.com and `export RUNWAYML_API_SECRET=...`")
        import requests  # noqa
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}", "X-Runway-Version": RUNWAY_VERSION,
                               "Content-Type": "application/json"})

    def _post(self, path: str, body: dict) -> str:
        r = self.s.post(f"{RUNWAY_BASE}/{path}", json=body, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"Runway {path} {r.status_code}: {r.text[:400]}")
        return r.json()["id"]

    def wait(self, task_id: str, label: str, every: float = 6.0, max_s: float = 900) -> str:
        t0 = time.time()
        while True:
            r = self.s.get(f"{RUNWAY_BASE}/tasks/{task_id}", timeout=60)
            r.raise_for_status()
            j = r.json()
            st = j.get("status")
            if st == "SUCCEEDED":
                return j["output"][0]
            if st in ("FAILED", "CANCELLED"):
                raise RuntimeError(f"{label}: task {st}: {j.get('failure') or j.get('failureCode') or j}")
            if time.time() - t0 > max_s:
                raise RuntimeError(f"{label}: timed out after {max_s}s (task {task_id})")
            print(f"    … {label}: {st} ({int(time.time() - t0)}s)", flush=True)
            time.sleep(every)

    def download(self, url: str, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.s.get(url, stream=True, timeout=300, headers={"Authorization": ""}) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)

    def organization(self) -> dict:
        """GET /organization — credit balance, tier and usage. Cheapest call to prove the key works."""
        r = self.s.get(f"{RUNWAY_BASE}/organization", timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Runway organization {r.status_code}: {r.text[:400]}")
        return r.json()

    def text_to_image(self, prompt: str, ratio: str = IMAGE_RATIO, reference: Optional[str] = None) -> str:
        body = {"model": IMAGE_MODEL, "promptText": prompt, "ratio": ratio}
        if reference:
            body["referenceImages"] = [{"uri": reference, "tag": "anchor"}]
            body["promptText"] = f"In the exact visual style and palette of @anchor. {prompt}"
        return self._post("text_to_image", body)

    def image_to_video(self, image_url: str, prompt: str, seconds: int, ratio: str = VIDEO_RATIO) -> str:
        return self._post("image_to_video", {"model": VIDEO_MODEL, "promptImage": image_url,
                                             "promptText": prompt[:1000], "ratio": ratio, "duration": seconds})


def generate(name: str, only: Optional[list[int]] = None, dry: bool = False):
    d = VIDEO_DIR / name
    sb = load_storyboard(name)
    todo = [s for s in sb["shots"] if s["clip"].endswith(f"{s['index']:02d}_{slugify(s['section'])}.mp4")]
    if only:
        todo = [s for s in todo if s["index"] in only]
    todo = [s for s in todo if not (d / s["clip"]).exists()]
    print(f"[generate] {name}: {len(todo)} clip(s) to make with {IMAGE_MODEL} → {VIDEO_MODEL}")
    if dry:
        for s in todo:
            print(f"  {s['clip']}  {s['clip_seconds']}s  {s['image_prompt'][:80]}…")
        return
    rw = Runway()
    anchor_url = sb.get("anchor_url")
    if not anchor_url:
        print("  anchor frame …")
        anchor_url = rw.wait(rw.text_to_image(sb["anchor_prompt"]), "anchor")
        rw.download(anchor_url, d / "frames" / "anchor.png")
        sb["anchor_url"] = anchor_url
        save_storyboard(name, sb)
    for s in todo:
        label = s["clip"]
        print(f"  {label}")
        img_url = s.get("frame_url")
        if not img_url:
            img_url = rw.wait(rw.text_to_image(s["image_prompt"], reference=anchor_url), f"{label} frame")
            rw.download(img_url, d / s["frame"])
            s["frame_url"] = img_url
            save_storyboard(name, sb)
        vid_url = rw.wait(rw.image_to_video(img_url, s["motion_prompt"], s["clip_seconds"]), f"{label} video")
        rw.download(vid_url, d / s["clip"])
        s["clip_url"] = vid_url
        save_storyboard(name, sb)
    print("[generate] done")


# ──────────────────────────────────────────────────────────────────────────────
# 4. ASSEMBLE (ffmpeg)
# ──────────────────────────────────────────────────────────────────────────────

def ff(*args, quiet=True) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error" if quiet else "info", *map(str, args)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("ffmpeg failed:\n" + " ".join(cmd) + "\n" + r.stderr[-2000:])


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        raise RuntimeError(f"ffprobe failed on {path}: {r.stderr}")
    return float(r.stdout.strip())


def find_font() -> Optional[str]:
    for c in ["/usr/share/fonts/truetype/lato/Lato-Semibold.ttf", "/usr/share/fonts/truetype/lato/Lato-Medium.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Helvetica.ttc"]:
        if Path(c).exists():
            return c
    return None


def ffpath(p: Path) -> str:
    """Path for use inside an ffmpeg filter string (Windows drive colons must be escaped)."""
    return str(p).replace("\\", "/").replace(":", "\\:")


def find_audio(d: Path, explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise SystemExit(f"audio not found: {p}")
        return p
    for ext in ("mp3", "wav", "m4a", "flac", "ogg", "aac"):
        p = d / f"audio.{ext}"
        if p.exists():
            return p
    return None


def load_timings(d: Path) -> Optional[dict]:
    p = d / "timings.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def segment_from_clip(clip: Path, dur: float, out: Path):
    """Loop/trim a Runway clip to exactly `dur` seconds at the project size, no audio."""
    ff("-stream_loop", "-1", "-i", clip, "-t", f"{dur:.3f}",
       "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},fps={FPS},format=yuv420p",
       "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out)


def segment_from_audio(audio: Path, start: float, dur: float, out: Path, kind: str, idx: int):
    """Fallback: waveform/spectrum visualizer for one section, tinted by section kind."""
    hue = {"intro": "0x1b2a49", "verse": "0x10233a", "prechorus": "0x2a1f4a", "chorus": "0x4a1030",
           "bridge": "0x0d2b2b", "interlude": "0x222222", "ident": "0x000000", "outro": "0x1a1a2e"}.get(kind, "0x101820")
    mode = "showwaves=s={w}x{h}:mode=cline:colors=white@0.85:rate={fps}" if kind in ("verse", "bridge", "intro", "outro") \
        else "showcqt=s={w}x{h}:fps={fps}:bar_g=2:sono_g=4:count=2:axis=0"
    vis = mode.format(w=WIDTH, h=HEIGHT, fps=FPS)
    ff("-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", audio,
       "-filter_complex",
       f"color=c={hue}:s={WIDTH}x{HEIGHT}:r={FPS}[bg];[0:a]{vis},format=rgba,colorchannelmixer=aa=0.9[v];"
       f"[bg][v]overlay=format=auto,format=yuv420p[out]",
       "-map", "[out]", "-t", f"{dur:.3f}", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", out)


def caption_filter(sb: dict, prompt: SunoPrompt, work: Path, mode: str) -> str:
    """drawtext chain: title card (first 4s), section label, and lyric lines spread across each section."""
    if mode == "none":
        return "null"
    font = find_font()
    fopt = f"fontfile='{ffpath(Path(font))}':" if font else ""
    parts = []

    def draw(text: str, t0: float, t1: float, size: int, y: str, x: str = "(w-text_w)/2", alpha: float = 0.92):
        tf = work / f"txt_{len(parts):03d}.txt"
        tf.write_text(text, encoding="utf-8")
        parts.append(
            f"drawtext={fopt}textfile='{ffpath(tf)}':fontsize={size}:fontcolor=white@{alpha}:"
            f"borderw=2:bordercolor=black@0.6:x={x}:y={y}:enable='between(t,{t0:.2f},{t1:.2f})'")

    total = sb["audio_seconds"] or sum(s["duration"] for s in sb["shots"])
    draw(prompt.title, 0.4, min(4.5, total), 64, "h*0.42")
    if mode in ("full", "lyrics"):
        for s, sec in zip(sb["shots"], prompt.sections):
            t0, t1 = s["start"], s["start"] + s["duration"]
            if mode == "full":
                draw(sec.name.upper(), t0 + 0.2, t1, 22, "h-60", x="40", alpha=0.6)
            lines = [l for l in sec.lines if l]
            if not lines:
                continue
            # group lines into couplets, spread evenly across the section
            groups = [lines[i:i + 2] for i in range(0, len(lines), 2)]
            per = (t1 - t0) / len(groups)
            for g, grp in enumerate(groups):
                a, b = t0 + g * per, t0 + (g + 1) * per - 0.15
                draw("\n".join(grp), a, b, 34, "h*0.78")
    return ",".join(parts) if parts else "null"


def assemble(name: str, audio_arg: Optional[str], captions: str = "full", force_fallback: bool = False):
    d = VIDEO_DIR / name
    prompt = parse_prompt(prompt_path(name))
    audio = find_audio(d, audio_arg)
    if audio is None:
        raise SystemExit(f"no audio for {name} — drop the Suno download at {d / 'audio.mp3'} or pass --audio. "
                         "(fail-closed: a storyboard is not a video)")
    if audio_arg and audio.parent != d:
        shutil.copy2(audio, d / f"audio{audio.suffix.lower()}")
        audio = d / f"audio{audio.suffix.lower()}"
    seconds = probe_duration(audio)
    sb = build_storyboard(prompt, seconds, load_timings(d))
    # keep runway urls from an earlier storyboard
    old = load_storyboard(name, missing_ok=True) or {}
    for k in ("anchor_url",):
        if k in old:
            sb[k] = old[k]
    for s, o in zip(sb["shots"], old.get("shots", [])):
        for k in ("frame_url", "clip_url"):
            if k in o:
                s[k] = o[k]
    save_storyboard(name, sb)

    work = d / "_work"
    work.mkdir(parents=True, exist_ok=True)
    segs, used_clips, fallback = [], 0, 0
    for s in sb["shots"]:
        seg = work / f"seg_{s['index']:02d}.mp4"
        clip = d / s["clip"]
        if clip.exists() and not force_fallback:
            segment_from_clip(clip, s["duration"], seg); used_clips += 1
        else:
            segment_from_audio(audio, s["start"], s["duration"], seg, s["kind"], s["index"]); fallback += 1
        segs.append(seg)
    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{ffpath(p) if os.name != 'nt' else str(p).replace(chr(92), '/')}'\n" for p in segs), encoding="utf-8")
    silent = work / "video_only.mp4"
    ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", silent)

    out = d / f"{name}.mp4"
    vf = caption_filter(sb, prompt, work, captions)
    ff("-i", silent, "-i", audio, "-vf", f"{vf},fade=t=in:st=0:d=1,fade=t=out:st={max(0, seconds - 2):.2f}:d=2",
       "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out)

    manifest = {"name": name, "title": prompt.title, "output": str(out.relative_to(ROOT)), "audio": str(audio.name),
                "audio_seconds": seconds, "sections": len(sb["shots"]), "runway_clips_used": used_clips,
                "fallback_segments": fallback, "captions": captions, "video_model": VIDEO_MODEL,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "toolkit_version": TOOLKIT_VERSION}
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    kind = "RUNWAY" if fallback == 0 else ("MIXED" if used_clips else "FALLBACK-VISUALIZER")
    print(f"[assemble] {out.relative_to(ROOT)}  {seconds:.1f}s  {used_clips} runway clip(s), {fallback} fallback  → {kind}")
    if fallback:
        print(f"           {fallback} section(s) had no clip in {d / 'clips'} — run `generate {name}` or drop clips there and re-assemble.")


# ──────────────────────────────────────────────────────────────────────────────
# 5. IO + CLI
# ──────────────────────────────────────────────────────────────────────────────

def prompt_path(name: str) -> Path:
    for c in (PROMPTS_DIR / f"{name}_suno.txt", PROMPTS_DIR / f"{name}.txt", Path(name)):
        if c.exists():
            return c
    raise SystemExit(f"no prompt named {name!r} in {PROMPTS_DIR}")


def all_names() -> list[str]:
    return sorted(re.sub(r"_suno$", "", p.stem) for p in PROMPTS_DIR.glob("*.txt"))


def save_storyboard(name: str, sb: dict):
    d = VIDEO_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "storyboard.json").write_text(json.dumps(sb, indent=2, ensure_ascii=False), encoding="utf-8")
    (d / "storyboard.md").write_text(storyboard_md(sb), encoding="utf-8")


def load_storyboard(name: str, missing_ok: bool = False) -> Optional[dict]:
    p = VIDEO_DIR / name / "storyboard.json"
    if not p.exists():
        if missing_ok:
            return None
        raise SystemExit(f"no storyboard for {name} — run `storyboard {name}` first")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_storyboard(names: list[str], audio_arg: Optional[str]):
    for n in names:
        p = parse_prompt(prompt_path(n))
        d = VIDEO_DIR / n
        audio = find_audio(d, audio_arg if len(names) == 1 else None)
        secs = probe_duration(audio) if audio else None
        sb = build_storyboard(p, secs, load_timings(d))
        old = load_storyboard(n, missing_ok=True) or {}
        for k in ("anchor_url",):
            if k in old:
                sb[k] = old[k]
        save_storyboard(n, sb)
        print(f"[storyboard] {n:40s} {len(p.sections):2d} sections → {sb['unique_shots']:2d} shots  "
              f"({sb['timing_source']})  → suno_video/{n}/storyboard.md")


def cmd_status():
    print(f"{'name':40s} {'sec':>3s} {'shots':>5s} {'clips':>5s} audio  video")
    for n in all_names():
        d = VIDEO_DIR / n
        sb = load_storyboard(n, missing_ok=True)
        if not sb:
            print(f"{n:40s}   -     -     -  -      -   (no storyboard)")
            continue
        have = sum(1 for s in sb["shots"] if (d / s["clip"]).exists())
        audio = "yes" if find_audio(d, None) else "no "
        video = "yes" if (d / f"{n}.mp4").exists() else "no "
        print(f"{n:40s} {sb['sections']:3d} {sb['unique_shots']:5d} {have:5d}  {audio}    {video}")


def cmd_runway_check() -> None:
    """Prove the Runway connection: key present → GET /organization → print tier + credits."""
    key = os.environ.get("RUNWAYML_API_SECRET")
    src = "environment" if key and not (ROOT / ".env").exists() else ".env" if key else None
    if not key:
        print("Runway: NOT CONNECTED")
        print(f"  Put your key in {ROOT / '.env'} as  RUNWAYML_API_SECRET=key_...  (gitignored)")
        print("  Create one at https://dev.runwayml.com → API Keys, then re-run:  runway check")
        raise SystemExit(2)
    try:
        org = Runway().organization()
    except Exception as e:  # noqa: BLE001
        print(f"Runway: key found ({src}) but the API rejected it → {e}")
        raise SystemExit(1)
    tier = org.get("tier") or {}
    credits = org.get("creditBalance")
    print("Runway: CONNECTED")
    print(f"  key source     {src}   (…{key[-4:]})")
    print(f"  credit balance {credits}")
    if tier:
        print(f"  tier           {json.dumps(tier)[:300]}")
    print(f"  models         image={IMAGE_MODEL}  video={VIDEO_MODEL}  ratio={VIDEO_RATIO}")
    print("  next           python tools/suno_video_infra.py generate <name>   (or `run <name> --audio track.mp3`)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("storyboard"); a.add_argument("name", nargs="+"); a.add_argument("--audio")
    g = sub.add_parser("generate"); g.add_argument("name"); g.add_argument("--only", type=int, nargs="*")
    g.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("assemble"); s.add_argument("name"); s.add_argument("--audio")
    s.add_argument("--captions", choices=["full", "lyrics", "title", "none"], default="full")
    s.add_argument("--fallback", action="store_true", help="ignore clips, force visualizer")
    r = sub.add_parser("run"); r.add_argument("name"); r.add_argument("--audio")
    r.add_argument("--captions", choices=["full", "lyrics", "title", "none"], default="full")
    r.add_argument("--no-runway", action="store_true")
    sub.add_parser("status")
    rw = sub.add_parser("runway", help="Runway connection: `runway check`")
    rw.add_argument("action", choices=["check"])
    args = ap.parse_args(argv)

    if args.cmd == "storyboard":
        names = all_names() if args.name == ["all"] else args.name
        cmd_storyboard(names, args.audio)
    elif args.cmd == "generate":
        if not (VIDEO_DIR / args.name / "storyboard.json").exists():
            cmd_storyboard([args.name], None)
        generate(args.name, args.only, args.dry_run)
    elif args.cmd == "assemble":
        assemble(args.name, args.audio, args.captions, args.fallback)
    elif args.cmd == "run":
        cmd_storyboard([args.name], args.audio)
        if not args.no_runway:
            if os.environ.get("RUNWAYML_API_SECRET"):
                generate(args.name)
            else:
                print("[run] RUNWAYML_API_SECRET not set — skipping Runway, assembling with the visualizer fallback")
        assemble(args.name, args.audio, args.captions)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "runway":
        cmd_runway_check()


if __name__ == "__main__":
    main()
