#!/usr/bin/env python3
"""
ecosystem_bus.py — LLM Governance Toolkit
The bus that connects every infra to the three hubs:

    BLOOMFIELD  (ChatGPT / OpenAI)   — the voice: drafts and speaks AS the user
    CLAUDE      (Anthropic)          — the red team: cuts, tightens, pre-filters before the user's own check
    SUNO                             — the sound: turns anything into a Suno prompt (→ suno_prompts/, → video infra)

Every infra slot (see infra/ecosystem.json) can put an Envelope on the bus; the bus
routes it to any subset of hubs, in order, threading the output of one hub into the
next (Bloomfield → Claude → Suno is the classic chain).

Governance carried by the bus (from the recorded decisions):
  • nothing enters the canonical tree unchecked — hub outputs land in the QUEUE, not the tree
  • the queue holds at most 3 unchecked items; generation stops when it is full
  • Claude red-teams BEFORE the user's own check (pre-filter), never instead of it
  • Bloomfield speaks as the user — the user's name/voice is on the output
  • X (inspiration) is triage-only: it may route, never generate — the bus enforces `may_generate`
  • Nova pardons a killed item, Timely pulls it back in — `pardon` / `resurrect` are separate steps
  • the goddess can overrule the user's check — `--as goddess` is the only way to bypass CHECK
  • one exclusion from "connect everything" was stated but never named — `excluded: []` in
    ecosystem.json is where it goes; the bus refuses to route to/from anything listed there

Fail-closed: a hub whose key is missing does not pretend — it writes what it WOULD send to
bus/outbox/<id>.<hub>.json and marks the envelope DRY. Nothing is silently skipped.

Commands
    python tools/ecosystem_bus.py map                                  # print the wiring
    python tools/ecosystem_bus.py send --from research --to bloomfield,claude,suno --text "…"
    python tools/ecosystem_bus.py send --from jotform --to all --file intake.txt --title "Kegyelem"
    python tools/ecosystem_bus.py queue                                # unchecked items (max 3)
    python tools/ecosystem_bus.py check   <id> [--verdict KEEP|CUT]    # the user's own check
    python tools/ecosystem_bus.py pardon  <id>                         # Nova
    python tools/ecosystem_bus.py resurrect <id>                       # Timely
    python tools/ecosystem_bus.py tree                                 # the canonical tree

Keys (env):  OPENAI_API_KEY   ANTHROPIC_API_KEY   (Suno has no public API: the Suno hub writes the
             prompt file in the repo's suno_prompts/ format and hands it to suno_video_infra.py)
Models (env, optional): BLOOMFIELD_MODEL (default gpt-4o)  BLOOMFIELD_VOICE (default onyx)
                        CLAUDE_MODEL (default claude-sonnet-4-5)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

TOOLKIT_VERSION = 37
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "infra" / "ecosystem.json"
BUS = ROOT / "bus"
QUEUE = BUS / "queue.json"
TREE = BUS / "tree.json"
KILLED = BUS / "killed.json"
OUTBOX = BUS / "outbox"
PROMPTS_DIR = ROOT / "suno_prompts"
QUEUE_BOUND = 3
SOVEREIGN = "gergo"

HUBS = ("bloomfield", "claude", "suno")


# ──────────────────────────────────────────────────────────────────────────────
# 1. REGISTRY
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_ECOSYSTEM = {
    "version": TOOLKIT_VERSION,
    "sovereign": SOVEREIGN,
    "apex": {"goddess": {"name": "Herczeg Viktória", "role": "apex — can overrule the sovereign's check",
                         "trigger": "the marriage"}},
    "hubs": {
        "bloomfield": {"model": "ChatGPT (OpenAI)", "role": "voice — drafts and speaks as the user", "status": "eventually"},
        "claude":     {"model": "Anthropic",        "role": "red team — pre-filter before the user's check", "status": "eventually"},
        "suno":       {"model": "Suno",             "role": "sound — anything → Suno prompt → track → video", "status": "live"},
    },
    "infras": {
        "bloomfield":   {"model": "ChatGPT",  "role": "voice",                          "may_generate": True},
        "red_team":     {"model": "Anthropic","role": "red team",                       "may_generate": True},
        "inspiration":  {"model": "X (Grok)", "role": "inspiration — triage only",      "may_generate": False},
        "research":     {"model": "DeepSeek", "role": "research — generates and checks","may_generate": True},
        "bosch":        {"model": None,       "role": "tool-building + rejection store","may_generate": True},
        "sap":          {"model": None,       "role": "management / ERP — assigns goals","may_generate": True},
        "timely":       {"model": None,       "role": "resurrection — executes Nova's pardon", "may_generate": False},
        "nova":         {"model": None,       "role": "clemency (kegyelem) — decides the pardon", "may_generate": False},
        "tex":          {"model": None,       "role": "slot held, function undecided",  "may_generate": True},
        "latex":        {"model": None,       "role": "slot held, function undecided",  "may_generate": True},
        "jotform":      {"model": "Jotform",  "role": "intake front-end",               "may_generate": True},
        "suno":         {"model": "Suno",     "role": "music",                          "may_generate": True},
        "video":        {"model": "Runway + free-tier AI-video services", "role": "video, under Suno", "may_generate": True},
    },
    "excluded": [],
    "wiring": "every infra → {bloomfield, claude, suno}; hubs chain in the order given",
}


def load_ecosystem() -> dict:
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(DEFAULT_ECOSYSTEM, indent=2, ensure_ascii=False), encoding="utf-8")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


# ──────────────────────────────────────────────────────────────────────────────
# 2. ENVELOPE + STORES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Hop:
    hub: str
    status: str            # OK | DRY | REFUSED | ERROR
    output: str = ""
    detail: str = ""
    at: str = ""


@dataclass
class Envelope:
    id: str
    source: str
    title: str
    text: str
    kind: str = "text"     # text | idea | lyric | form | paper
    actor: str = SOVEREIGN
    hops: list[Hop] = field(default_factory=list)
    state: str = "UNCHECKED"   # UNCHECKED | CANONICAL | KILLED | PARDONED
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    @property
    def current(self) -> str:
        """Latest textual output on the envelope — what the next hub works on."""
        for h in reversed(self.hops):
            if h.status == "OK" and h.output:
                return h.output
        return self.text


def _load(p: Path) -> list[dict]:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _save(p: Path, items: list[dict]):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower() or "item"


# ──────────────────────────────────────────────────────────────────────────────
# 3. HUBS
# ──────────────────────────────────────────────────────────────────────────────

def _dry(env: Envelope, hub: str, payload: dict, why: str) -> Hop:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    (OUTBOX / f"{env.id}.{hub}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return Hop(hub, "DRY", "", f"{why} — request written to bus/outbox/{env.id}.{hub}.json", time.strftime("%H:%M:%S"))


class BloomfieldHub:
    """ChatGPT — speaks as the user. Draft in the user's voice; optionally TTS to bus/voice/<id>.mp3."""
    name = "bloomfield"

    def __init__(self, voice_out: bool = False):
        self.key = os.environ.get("OPENAI_API_KEY")
        self.model = os.environ.get("BLOOMFIELD_MODEL", "gpt-4o")
        self.voice = os.environ.get("BLOOMFIELD_VOICE", "onyx")
        self.voice_out = voice_out

    def run(self, env: Envelope) -> Hop:
        system = (f"You are Bloomfield, the voice infra of {env.actor}'s governance ecosystem. You speak AS {env.actor}, "
                  f"first person, their name on the output — not as an assistant. Keep their mix of Hungarian and English "
                  f"where it appears. Draft the item below in their voice; if it is lyrics or a Suno prompt, keep the "
                  f"bracketed section structure. Output only the draft.")
        body = {"model": self.model, "messages": [{"role": "system", "content": system},
                                                  {"role": "user", "content": f"[{env.kind} from {env.source}] {env.title}\n\n{env.current}"}],
                "temperature": 0.8}
        if not self.key:
            return _dry(env, self.name, body, "OPENAI_API_KEY not set")
        import requests
        r = requests.post("https://api.openai.com/v1/chat/completions", json=body, timeout=120,
                          headers={"Authorization": f"Bearer {self.key}"})
        if r.status_code >= 400:
            return Hop(self.name, "ERROR", "", r.text[:400], time.strftime("%H:%M:%S"))
        out = r.json()["choices"][0]["message"]["content"].strip()
        detail = f"{self.model}"
        if self.voice_out:
            a = requests.post("https://api.openai.com/v1/audio/speech", timeout=300,
                              headers={"Authorization": f"Bearer {self.key}"},
                              json={"model": "gpt-4o-mini-tts", "voice": self.voice, "input": out[:4000]})
            if a.status_code < 400:
                p = BUS / "voice" / f"{env.id}.mp3"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(a.content)
                detail += f", spoken → {p.relative_to(ROOT)}"
            else:
                detail += f", TTS failed: {a.text[:120]}"
        return Hop(self.name, "OK", out, detail, time.strftime("%H:%M:%S"))


class ClaudeHub:
    """Anthropic — the red team. Returns the tightened text plus a verdict block. Pre-filter, not the check."""
    name = "claude"

    def __init__(self):
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        self.model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")

    def run(self, env: Envelope) -> Hop:
        system = (f"You are the red team of {env.actor}'s governance ecosystem. Your job is a PRE-FILTER before "
                  f"{env.actor}'s own check — you never approve into the canonical tree. Tear the item apart: name every "
                  f"weak line, unsupported claim, cliché, or contradiction; then return a tightened version that keeps "
                  f"the author's voice and structure. Format:\n\n"
                  f"VERDICT: KEEP | TIGHTEN | CUT\nNOTES:\n- …\n\n---\n<tightened item>")
        body = {"model": self.model, "max_tokens": 4000, "system": system,
                "messages": [{"role": "user", "content": f"[{env.kind} from {env.source}] {env.title}\n\n{env.current}"}]}
        if not self.key:
            return _dry(env, self.name, body, "ANTHROPIC_API_KEY not set")
        import requests
        r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=180,
                          headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"})
        if r.status_code >= 400:
            return Hop(self.name, "ERROR", "", r.text[:400], time.strftime("%H:%M:%S"))
        full = "".join(b.get("text", "") for b in r.json()["content"]).strip()
        verdict = re.search(r"VERDICT:\s*(\w+)", full)
        tightened = full.split("\n---\n", 1)[1].strip() if "\n---\n" in full else full
        return Hop(self.name, "OK", tightened, f"{self.model} verdict={verdict.group(1) if verdict else '?'}\n{full.split(chr(10) + '---' + chr(10))[0]}",
                   time.strftime("%H:%M:%S"))


class SunoHub:
    """Suno — no public API. The hub writes suno_prompts/<slug>_suno.txt in the repo's own format
    (title / [Style:] / [Section - cue] / lyrics) and runs the video infra's storyboard on it.
    If the current text already looks like a Suno prompt it is written as-is; otherwise it is
    wrapped as spoken word over electronic (the house default) so it can be pasted into Suno."""
    name = "suno"

    def run(self, env: Envelope) -> Hop:
        text = env.current
        slug = slugify(env.title)
        dest = PROMPTS_DIR / f"{slug}_suno.txt"
        if dest.exists():
            slug = f"{slug}_{env.id[:6]}"
            dest = PROMPTS_DIR / f"{slug}_suno.txt"
        if "[Style:" not in text:
            lines = [l for l in text.splitlines() if l.strip()]
            body = "\n".join(lines)
            text = (f"{env.title}\n\n[Style: spoken word over downtempo electronic, 88 BPM, warm sub bass, tape hiss, "
                    f"male voice calm and close to the mic, minor key, Hungarian and English mixed]\n\n"
                    f"[Intro - low pad, one held chord]\n\n[Verse 1 - spoken, unhurried]\n{body}\n\n"
                    f"[Chorus - beat opens]\n{env.title}\n\n[Outro - beat fades]\n")
        elif not text.splitlines()[0].strip() or text.lstrip().startswith("["):
            text = f"{env.title}\n\n{text}"
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        dest.write_text(text.strip() + "\n", encoding="utf-8")
        detail = f"wrote {dest.relative_to(ROOT)}"
        vi = ROOT / "tools" / "suno_video_infra.py"
        if vi.exists():
            r = subprocess.run([sys.executable, str(vi), "storyboard", slug], capture_output=True, text=True)
            detail += " · " + (r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else f"storyboard failed: {r.stderr[-200:]}")
        return Hop(self.name, "OK", text, detail, time.strftime("%H:%M:%S"))


def make_hub(name: str, voice_out: bool):
    return {"bloomfield": lambda: BloomfieldHub(voice_out), "claude": ClaudeHub, "suno": SunoHub}[name]()


# ──────────────────────────────────────────────────────────────────────────────
# 4. ROUTING (the governance lives here)
# ──────────────────────────────────────────────────────────────────────────────

def route(env: Envelope, hubs: list[str], eco: dict, voice_out: bool = False) -> Envelope:
    excluded = set(eco.get("excluded", []))
    if env.source in excluded or any(h in excluded for h in hubs):
        raise SystemExit(f"refused: {env.source} or one of {hubs} is in ecosystem.json excluded={sorted(excluded)}")
    infra = eco["infras"].get(env.source)
    if infra is None:
        raise SystemExit(f"unknown infra {env.source!r}; known: {', '.join(eco['infras'])}")
    if not infra.get("may_generate", True) and ({"suno", "bloomfield"} & set(hubs)):
        raise SystemExit(f"refused: {env.source} is triage-only (may_generate=false) — it may route to claude, not generate via bloomfield/suno")
    queue = _load(QUEUE)
    unchecked = [q for q in queue if q["state"] == "UNCHECKED"]
    if len(unchecked) >= QUEUE_BOUND and env.actor != "goddess":
        raise SystemExit(f"queue full: {len(unchecked)} unchecked items (bound {QUEUE_BOUND}) — run `check` before generating more")
    for h in hubs:
        hub = make_hub(h, voice_out)
        try:
            hop = hub.run(env)
        except Exception as e:  # network etc.
            hop = Hop(h, "ERROR", "", str(e)[:400], time.strftime("%H:%M:%S"))
        env.hops.append(hop)
        print(f"  [{h:10s}] {hop.status:7s} {hop.detail.splitlines()[0] if hop.detail else ''}")
        if hop.status in ("ERROR", "REFUSED"):
            print("  chain stopped (fail-closed)")
            break
    queue.append(asdict(env))
    _save(QUEUE, queue)
    return env


def cmd_send(a, eco):
    text = Path(a.file).read_text(encoding="utf-8") if a.file else a.text
    if not text:
        raise SystemExit("--text or --file required")
    hubs = list(HUBS) if a.to == "all" else [h.strip() for h in a.to.split(",")]
    bad = [h for h in hubs if h not in HUBS]
    if bad:
        raise SystemExit(f"unknown hub(s) {bad}; hubs are {HUBS}")
    title = a.title or (text.strip().splitlines()[0][:60] if text.strip() else "untitled")
    env = Envelope(uuid.uuid4().hex[:12], a.source, title, text, a.kind, a.actor)
    print(f"[bus] {env.id}  {env.source} → {' → '.join(hubs)}   \"{title}\"")
    env = route(env, hubs, eco, a.speak)
    print(f"[bus] queued as UNCHECKED ({sum(1 for q in _load(QUEUE) if q['state']=='UNCHECKED')}/{QUEUE_BOUND}). "
          f"Next: python tools/ecosystem_bus.py check {env.id}")
    if a.show:
        print("\n" + env.current)


def cmd_queue(_a, _eco):
    q = _load(QUEUE)
    if not q:
        print("queue empty")
    for it in q:
        hops = " → ".join(f"{h['hub']}:{h['status']}" for h in it["hops"])
        print(f"{it['id']}  {it['state']:9s} {it['source']:12s} {it['title'][:40]:40s} {hops}")


def _move(id_: str, from_state: str, to_state: str, src: Path, dst: Optional[Path]) -> dict:
    items = _load(src)
    it = next((x for x in items if x["id"].startswith(id_)), None)
    if not it:
        raise SystemExit(f"{id_} not in {src.name}")
    if from_state and it["state"] != from_state:
        raise SystemExit(f"{id_} is {it['state']}, expected {from_state}")
    items.remove(it)
    _save(src, items)
    it["state"] = to_state
    it.setdefault("history", []).append({"to": to_state, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    if dst is not None:
        d = _load(dst); d.append(it); _save(dst, d)
    return it


def cmd_check(a, _eco):
    """The user's own check — the only door into the canonical tree (besides the goddess)."""
    if a.verdict == "KEEP":
        it = _move(a.id, "UNCHECKED", "CANONICAL", QUEUE, TREE)
        print(f"[check] {it['id']} → canonical tree ({len(_load(TREE))} items)")
    else:
        it = _move(a.id, "UNCHECKED", "KILLED", QUEUE, KILLED)
        print(f"[check] {it['id']} → killed (Bosch's rejection store: bus/killed.json). Nova may pardon it.")


def cmd_pardon(a, _eco):
    """Nova decides the kill was wrong. Does NOT move the item — Timely does that."""
    items = _load(KILLED)
    it = next((x for x in items if x["id"].startswith(a.id)), None)
    if not it:
        raise SystemExit(f"{a.id} not in killed store")
    it["state"] = "PARDONED"
    it.setdefault("history", []).append({"to": "PARDONED", "by": "nova", "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _save(KILLED, items)
    print(f"[nova] {it['id']} pardoned — kegyelem. Run `resurrect {it['id']}` (Timely) to pull it back.")


def cmd_resurrect(a, _eco):
    it = _move(a.id, "PARDONED", "CANONICAL", KILLED, TREE)
    print(f"[timely] {it['id']} resurrected → canonical tree")


def cmd_tree(_a, _eco):
    t = _load(TREE)
    print(f"canonical tree: {len(t)} item(s)")
    for it in t:
        print(f"  {it['id']}  {it['source']:12s} {it['title'][:50]}")


def cmd_map(_a, eco):
    print(f"ECOSYSTEM v{eco['version']} — sovereign: {eco['sovereign']}   apex: goddess ({eco['apex']['goddess']['name']})")
    print("\nHUBS")
    for k, v in eco["hubs"].items():
        print(f"  {k:11s} {v['model']:18s} {v['role']}  [{v['status']}]")
    print("\nINFRAS → every hub" + (f"   (excluded: {eco['excluded']})" if eco["excluded"] else "   (exclusion slot empty — not yet named)"))
    for k, v in eco["infras"].items():
        gen = "generate+route" if v.get("may_generate", True) else "route only   "
        print(f"  {k:12s} {str(v['model'] or '—'):36s} {gen}  {v['role']}")
    print("\nKEYS  OPENAI_API_KEY=" + ("set" if os.environ.get("OPENAI_API_KEY") else "MISSING (bloomfield → DRY)") +
          "  ANTHROPIC_API_KEY=" + ("set" if os.environ.get("ANTHROPIC_API_KEY") else "MISSING (claude → DRY)") +
          "  suno: file hub (no API)")
    print(f"QUEUE {sum(1 for q in _load(QUEUE) if q['state']=='UNCHECKED')}/{QUEUE_BOUND} unchecked · TREE {len(_load(TREE))} · KILLED {len(_load(KILLED))}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--from", dest="source", required=True)
    s.add_argument("--to", default="all", help="comma list of bloomfield,claude,suno or 'all' (chained in order)")
    s.add_argument("--text"); s.add_argument("--file"); s.add_argument("--title")
    s.add_argument("--kind", default="text", choices=["text", "idea", "lyric", "form", "paper"])
    s.add_argument("--speak", action="store_true", help="Bloomfield also renders TTS to bus/voice/")
    s.add_argument("--as", dest="actor", default=SOVEREIGN, choices=[SOVEREIGN, "goddess"],
                   help="`--as goddess` bypasses the queue bound (apex override)")
    s.add_argument("--show", action="store_true", help="print the final text")
    sub.add_parser("queue"); sub.add_parser("tree"); sub.add_parser("map")
    c = sub.add_parser("check"); c.add_argument("id"); c.add_argument("--verdict", default="KEEP", choices=["KEEP", "CUT"])
    p = sub.add_parser("pardon"); p.add_argument("id")
    r = sub.add_parser("resurrect"); r.add_argument("id")
    a = ap.parse_args(argv)
    eco = load_ecosystem()
    {"send": cmd_send, "queue": cmd_queue, "check": cmd_check, "pardon": cmd_pardon,
     "resurrect": cmd_resurrect, "tree": cmd_tree, "map": cmd_map}[a.cmd](a, eco)


if __name__ == "__main__":
    main()
