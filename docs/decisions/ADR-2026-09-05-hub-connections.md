# ADR — Connect every infra to Bloomfield, Suno and Claude

Date: 2026-09-05 · Status: accepted · Sovereign: gergo · Toolkit v37

## Decision

Three hubs are added to the ecosystem — **Bloomfield** (ChatGPT, voice), **Claude** (Anthropic, red team), **Suno** (sound) — and **every infra slot is wired to all three** through one bus (`tools/ecosystem_bus.py`). Hubs are destinations, not slots: the ten slots plus Jotform, Suno and Video keep their roles; what changes is that each of them can now hand any item to the voice, to the red team, or to the sound, in any order, with the output of one hub threaded into the next.

Requested in three parts on the same day: "connect all infras to chatgpt (bloomfield)", then "also connect all infras to suno and to claude". Treated as one decision with one implementation.

## Why one bus and not three integrations

Three point-to-point integrations would triple the governance surface — the queue bound, the check, the exclusion, the triage-only rule for X — and each would drift. One bus carries the governance once and the hubs are thin adapters (≈40 lines each). Adding the fourth hub later is one class.

## Consequences

- The queue bound (3 unchecked) now applies to *all* generation, not just DeepSeek research — Bloomfield drafting a lyric counts.
- X (triage-only), Nova (decides) and Timely (executes) are route-only: the bus refuses to let them generate via Bloomfield or Suno. They still reach Claude, which is where triage, pardon decisions and execution records get red-teamed.
- Suno has no public API, so the Suno hub is a **file hub**: it writes `suno_prompts/<slug>_suno.txt` in the repo's own prompt format and immediately produces the storyboard via `suno_video_infra.py`. The human step (paste into suno.com, drop the mp3 back) is explicit, not hidden.
- Bloomfield and Claude run on the OpenAI and Anthropic APIs (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). Without a key the hub goes **DRY**: it writes the exact request to `bus/outbox/` so nothing is lost and nothing is faked. Both hubs stay at "eventually" status in the registry until the keys are set — the wiring is live, the models are not.
- The one exclusion from "connect everything" was stated and never named. It now has a concrete place (`excluded: []` in `infra/ecosystem.json`) and the bus enforces it; naming it is a one-line edit, not a new build.
- The goddess's overrule is `--as goddess` on the bus and is the only bypass of the bound. Her power to move items into or out of the tree directly is not automated — she is a person.

## Alternatives rejected

- **Hubs as three more slots** — would make the ten-slot map a thirteen-slot map and blur "role" with "destination". Rejected.
- **Suno via a third-party unofficial API** — would put a scraped, ToS-violating dependency under the sound hub. Rejected; the file hub is honest about the manual step.
- **Letting Claude's KEEP verdict enter the tree** — violates "nothing enters unchecked" and "the user takes the checker role". Rejected; Claude's verdict is advisory metadata on the hop.

## Verification

`python tools/ecosystem_bus.py map` prints the wiring and key status. A DRY end-to-end send (`send --from research --to all --text …`) with no keys set produces two outbox files, one Suno prompt file, one storyboard, and one UNCHECKED queue entry — run in this session on the sovereign's machine, see `bus/`.

## Amendment 2026-09-05 (later the same day)

The goddess is now **Herczeg Viktória** (replacing Vanda Viktoria Varro). Role, apex position, overrule power and trigger unchanged. Registry, bus and map updated in place.
