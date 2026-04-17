# instagram-reels-analysis

A Claude Code skill that produces a full deep-dive on an Instagram Business/Creator account: ranked leaderboard of every reel, local Whisper transcripts of the top 25, caption/hook/CTA pattern analysis, and an interactive HTML dashboard — all from one Windsor.ai API key.

## What it does

1. Pulls every post via the **Windsor.ai Instagram connector** (API-chunked in 2-week windows to work around Windsor's wide-join hang bug)
2. Ranks reels by a weighted composite score: **comments 30% · views 25% · saves 20% · shares 15% · likes 10%**
3. Downloads the top 25 reels in parallel
4. Extracts audio with `ffmpeg` and transcribes locally with **OpenAI Whisper** (`small.en` by default)
5. Builds a CSV spreadsheet, a JSON dataset, and an **interactive HTML dashboard** (warm paper aesthetic, Fraunces serif, Chart.js visualizations)
6. Opens the dashboard in your default browser

The dashboard includes: 6-KPI overview row, stacked-engagement bar chart, duration × score scatter, sortable leaderboard with expandable transcripts, full ranked post table with search, keyword density (written vs. spoken), and hook pattern frequency analysis.

## Install

### Option A — .skill file
Download `instagram-reels-analysis.skill` from Releases, then:
```bash
unzip instagram-reels-analysis.skill -d ~/.claude/skills/
```

### Option B — clone
```bash
git clone https://github.com/<you>/instagram-reels-analysis ~/.claude/skills/instagram-reels-analysis
```

## Requirements

- `python3` · `curl` · `ffmpeg`
- `whisper` CLI (`pip install -U openai-whisper` or `brew install openai-whisper`)
- A [Windsor.ai](https://windsor.ai) Instagram connector API key linked to a Business/Creator account
- ~1 GB free disk space for downloaded videos + transcripts

## Usage

Just say one of these in Claude Code:
- *"analyze my Instagram reels"*
- *"Instagram deep dive"*
- *"find my top performing reels"*
- *"what's working on my Instagram"*

Claude looks for a Windsor API key in this order:
1. `WINDSOR_API_KEY=...` in a `.env` file in the current directory
2. `~/.config/windsor/api_key` or `~/.windsor_api_key`
3. `$WINDSOR_API_KEY` environment variable
4. Prompts you to paste it

Or invoke the orchestrator directly:
```bash
python3 ~/.claude/skills/instagram-reels-analysis/scripts/run_analysis.py \
  --api-key "$WINDSOR_API_KEY" \
  --output-dir ./instagram-analysis \
  --top-n 25
```

### Flags
| Flag | Default | Notes |
|------|---------|-------|
| `--api-key` | *(required)* | Windsor.ai Instagram connector key |
| `--output-dir` | *(required)* | Where to write artifacts |
| `--top-n` | `25` | How many reels to download + transcribe |
| `--whisper-model` | `small.en` | `tiny.en`/`base.en`/`small.en`/`medium.en` — tradeoff speed vs. quality |
| `--date-from` | auto (2 yrs back) | `YYYY-MM-DD` |
| `--date-to` | today | `YYYY-MM-DD` |

### Runtime
- Windsor fetches: 5–15 min (chunked, with cooldowns)
- Downloads: 1–2 min for 25 reels
- Whisper transcription: ~2 min per reel with `small.en` on CPU (~50 min for 25 reels)
- Report + dashboard: <10 sec

**Total ~1 hour** for a typical account. Script prints progress.

## Output

```
<output-dir>/
├── output/
│   ├── dashboard.html         ← opens automatically
│   ├── reels_analysis.csv
│   ├── reels_data.json
│   └── summary.json
├── videos/*.mp4               ← downloaded top-N reels
├── audio/*.mp3
├── transcripts/*.json         ← Whisper output with per-segment timestamps
└── data/                      ← raw Windsor responses (cached; safe to delete)
```

## Known gotchas (codified into the orchestrator)

- **Windsor's `media_reel_video_views` is broken** — values come back ~10,000× too high due to a row-duplication join bug. The orchestrator uses `media_views` instead (the real Instagram reel-view metric).
- **Wide-date-range queries hang** — queries spanning more than ~30 days silently stop responding even though they return `200 OK` headers. The orchestrator splits into 2-week chunks with 5-second cooldowns.
- **Personal accounts won't work** — Instagram insights only populate for Business/Creator accounts. If `media_views` comes back all zeros, that's why.

## License

MIT — use and modify freely.
