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

## Credentials — `.env` ONLY

**NEVER ask the user to paste their Windsor API key into the chat, and NEVER accept it as a command-line flag.** Chat messages end up in logs and the agent transcript; CLI flags end up in shell history and `ps aux`. Both vectors leak keys.

The only supported way to pass the key is a local `.env` file:

```
WINDSOR_API_KEY=their_actual_key_value
```

### Resolve the key in this order, without ever seeing its value

1. Check if `.env` exists in the user's current working directory AND contains a non-empty `WINDSOR_API_KEY=...` line. If yes → proceed to run the script.
2. If `.env` doesn't exist or is missing the key, **write instructions for the user** — do not run the script and do not ask the user to paste the key. Tell them verbatim:

   > I need a Windsor.ai Instagram API key to run the analysis. For security, I can't accept the key in chat — it would end up in logs. Please:
   >
   > 1. Create a file named `.env` in this directory
   > 2. Add this single line (replace with your actual key): `WINDSOR_API_KEY=your_key_here`
   > 3. Tell me once you've saved it, and I'll run the analysis.
   >
   > You can get a key at https://windsor.ai (Instagram connector).

3. When the user confirms `.env` is saved, verify it exists (via `ls` / file check) — do NOT cat or print the file contents. Then run the script.

If the user tries to paste the key anyway, **refuse and re-send the `.env` instructions**. Do not acknowledge the pasted value, do not repeat it, do not store it in memory.

## Other inputs (safe to ask in chat)

- **Output directory** — defaults to `./instagram-analysis` in the current working directory. Ask if the user wants somewhere else.
- **Top-N to transcribe** — defaults to 25. Larger N means more transcription time (~2 min per reel with Whisper `small.en`).
- **Date range** — defaults to auto-detect (~2 years back to today).

**Do NOT ask for the Instagram handle** — the Windsor API key is already bound to a specific account.

## How to run the skill

Use the orchestrator script. It auto-loads `.env` from the current directory and handles everything else: API fetches with proper chunking (Windsor hangs on wide date ranges), download, transcription, ranking, report generation, and opening the dashboard.

```bash
python3 ~/.claude/skills/instagram-reels-analysis/scripts/run_analysis.py \
  --output-dir ./instagram-analysis \
  --top-n 25
```

Notes:
- The script reads `WINDSOR_API_KEY` from `./.env` automatically — no flag needed.
- If `.env` is elsewhere, pass `--env-file /path/to/.env`.
- The script **explicitly refuses** a `--api-key` flag to prevent shell-history leaks.

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
