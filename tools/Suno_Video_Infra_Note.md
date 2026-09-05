# Suno Video Infra — note

`tools/suno_video_infra.py` turns a Suno prompt into a video. It is the downstream of the Suno hub on the ecosystem bus and the place where the free-tier AI-video services attach (Runway first; the others are the same two calls — image → clip).

## Flow

```
suno_prompts/<name>_suno.txt
      │  parse: title / [Style: …] / [Section - cue] / lyrics / (performance notes)
      ▼
suno_video/<name>/storyboard.{json,md}      one shot per section; repeated sections share a shot
      │  Style keywords → visual language (pirate radio → booth, VU meters…; orchestral → stone hall…)
      │  Section kind  → camera motion (verse = slow track, chorus = wide sweep, ident = hard cut)
      │  Lyric lines   → mood line in the image prompt
      ▼
generate  (RUNWAYML_API_SECRET)   anchor frame → per-shot frame (@anchor reference) → image_to_video clip
      │                            gen4_image → gen4.5 (env SUNO_VIDEO_VIDEO_MODEL), 5 s or 10 s per shot
      ▼
assemble  (ffmpeg)                clips looped/trimmed to section length, concatenated, Suno audio muxed,
                                  title card + section label + lyric couplets burned in, fade in/out
      │                            no clip for a section → waveform (verses) / CQT spectrum (choruses) fallback
      ▼
suno_video/<name>/<name>.mp4  +  manifest.json
```

## Use

```bash
python tools/suno_video_infra.py storyboard all                       # every prompt → storyboard.md
python tools/suno_video_infra.py generate  agi_radio_001_sign_on      # Runway (needs key); --dry-run lists the calls
python tools/suno_video_infra.py assemble  agi_radio_001_sign_on --audio ~/Downloads/agi-radio.mp3
python tools/suno_video_infra.py run       agi_radio_001_sign_on --audio ~/Downloads/agi-radio.mp3   # all three
python tools/suno_video_infra.py status
```

Manual Runway path (free tier on runway.com, no API): open `storyboard.md`, paste each *image prompt* into Text/Image-to-Image, then the *motion prompt* into Image-to-Video, save the clip as `suno_video/<name>/clips/NN_<slug>.mp4` (names are in the storyboard), then `assemble`.

## Timing

Section lengths are allocated from the audio duration by section weight (verse 1.2 × lines, chorus 1.0, intro 0.6, ident 0.25 …). For exact cuts put `timings.json` next to the audio with section start seconds — `{"Intro": 0, "Verse 1": 6.5, "Pre-Chorus": 31.0, …}` — and re-assemble.

## Rules

- Fail-closed: no audio → no video (a storyboard is not a video). No key → `generate` refuses; `assemble` falls back and *says so* in the manifest (`fallback_segments`).
- Repeated sections (Chorus ×2, Station ident) reuse one clip — 9 sections cost 8 generations, 12 cost ~10.
- Captions default to `full` (title + section + lyrics); `--captions lyrics|title|none`.
- Output 1280×720, 24 fps, H.264 + AAC, faststart. Change `WIDTH/HEIGHT/VIDEO_RATIO` for vertical (720:1280 / 1080:1920).

## Smoke test (2026-09-05)

Ran on the sovereign's machine with a synthetic 40 s tone as `audio.mp3`: 11 prompts → 11 storyboards; `agi_radio_001_sign_on` assembled in 27 s, 9 fallback segments, captions verified frame-by-frame. Moved to `suno_video/_smoke_test/` — replace with the real Suno mp3.
