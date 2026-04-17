---
name: instagram-reels-analysis
description: End-to-end Instagram Reels performance analysis. Pulls every post for an Instagram Business/Creator account via the Windsor.ai connector, ranks reels by a weighted composite of comments/views/saves/shares/likes, downloads the top performers, transcribes them locally with Whisper, and builds an interactive HTML dashboard + CSV + written analysis. Use when the user says "analyze my Instagram reels", "Instagram deep dive", "look at my analytics", "find my top performing reels", "what's working on my Instagram", "transcribe my top videos", or provides a Windsor API key and asks for reels analytics.
---

# Instagram Reels Analysis

This skill produces a full deep-dive on an Instagram Business/Creator account: ranked leaderboard, transcripts of the top performers, caption/hook/CTA analysis, and an interactive dashboard — all from one API key.

## Requirements the user's machine must have

- `python3` · `curl` · `ffmpeg` · `whisper` (the `openai-whisper` CLI, installed via `pip` or `brew install openai-whisper`)
- A Windsor.ai Instagram connector API key connected to the target account
- ~1 GB free disk space for downloaded videos + transcripts

## Inputs to collect from the user

You need **one** thing to run this:

1. **Windsor.ai API key** — resolve in this order:
   - Check for `.env` in the current working directory containing `WINDSOR_API_KEY=...`
   - Check for `~/.config/windsor/api_key` or `~/.windsor_api_key`
   - Check for `$WINDSOR_API_KEY` in the environment
   - If none found, ask the user: *"I need your Windsor.ai Instagram API key. Paste it here, or save it to `.env` as `WINDSOR_API_KEY=...` and I'll read it from there."*

Optional:
- **Output directory** — defaults to `./instagram-analysis` in the current working directory. Ask if the user wants somewhere else.
- **Top-N to transcribe** — defaults to 25. Larger N means more transcription time (~2 min per reel with Whisper `small.en`).
- **Date range** — defaults to auto-detect (~2 years back to today).

**Do NOT ask for the Instagram handle** — the Windsor API key is already bound to a specific account.

## How to run the skill

Use the orchestrator script. It handles everything: API fetches with proper chunking (Windsor hangs on wide date ranges), download, transcription, ranking, report generation, and opening the dashboard.

```bash
python3 ~/.claude/skills/instagram-reels-analysis/scripts/run_analysis.py \
  --api-key "$WINDSOR_API_KEY" \
  --output-dir ./instagram-analysis \
  --top-n 25
```

Or if the user saved the key to `.env`:

```bash
export $(grep -v '^#' .env | xargs) && \
python3 ~/.claude/skills/instagram-reels-analysis/scripts/run_analysis.py \
  --api-key "$WINDSOR_API_KEY" \
  --output-dir ./instagram-analysis
```

### Expected runtime

- Windsor fetches: **5–15 min** depending on account history length (chunked in 2-week windows, ~10s cooldown between chunks to avoid rate-limit hangs)
- Video downloads: **1–2 min** for 25 reels
- Whisper transcription: **~2 min per reel** (50 min for 25 reels with `small.en` on CPU; faster with `tiny.en` or `base.en`)
- Report build + dashboard: **<10 sec**

**Total: ~1 hour for a typical account.** Run in the background if the user wants to keep chatting. The script prints progress as it goes.

## What gets produced

In `<output-dir>/output/`:
- `dashboard.html` — interactive dashboard (warm paper aesthetic, brick-red accent, Fraunces serif)
- `reels_analysis.csv` — full spreadsheet of every post with captions, hooks, CTAs, transcripts, all metrics
- `reels_data.json` — same data as JSON
- `summary.json` — aggregate stats

In `<output-dir>/`:
- `videos/*.mp4` — downloaded top-N reels
- `audio/*.mp3` — extracted audio
- `transcripts/*.json` — Whisper transcripts with per-segment timestamps
- `data/` — raw Windsor API responses (cached; deletable)

The dashboard auto-opens in the user's default browser at the end.

## After the script runs, write an analysis

Once the pipeline completes, read `output/reels_data.json` and `output/summary.json` and write an **`analysis.md`** to the output directory with these sections:

1. **Executive summary** — top 3 findings in plain language (not metric descriptions — actual strategic insights)
2. **Top-25 leaderboard** — markdown table with rank, date, duration, views, comments, saves, shares, likes, topic, hook type
3. **Duration analysis** — buckets of <30s / 30–45s / 45–60s / >60s, which wins
4. **Subject matter** — content pillar breakdown (what topics dominate the top 25?)
5. **Hook patterns** — which opening formats drive the most comments
6. **CTA mechanics** — how do top reels close? (look for `comment [keyword]` funnels, "link in bio", etc.)
7. **Posting cadence** — which months produced the most top-25 content
8. **Recommendations** — 5–7 concrete moves for the next 30 days, each tied to a specific observation from the data

Be direct and specific. Name reels by shortcode or date. Quote actual hooks. Don't hedge.

## Troubleshooting

- **Windsor query hangs >3 min**: the API doesn't respect `--max-time` under load. The orchestrator already chunks by 2-week windows with cooldowns; if it still hangs, tell the user to check account rate limits at windsor.ai.
- **`media_views` all zero**: account might be Personal (not Business/Creator). Instagram insights only populate for Business/Creator accounts.
- **No rows returned**: the Windsor connector may not be linked to the account. User needs to go to windsor.ai → Instagram connector → re-authenticate.
- **Whisper too slow**: use `--whisper-model tiny.en` or `base.en` for 3–5× speed-up at some accuracy cost. `small.en` is the default because it gets hooks and CTAs right; `tiny.en` occasionally mangles them.
- **`media_reel_video_views` has 10,000× inflated values**: known Windsor join bug — use `media_views` instead (the orchestrator already does this).

## Design principles for the dashboard

The template is already designed — don't redesign unless asked. Palette: warm cream paper `#f7f4ec`, brick-red `#a8322a` accent, Fraunces serif for display + numbers, Instrument Sans for UI, JetBrains Mono for tabular data. Preserve this look when iterating.
