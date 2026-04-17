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

### 1. Put your Windsor API key in a `.env` file

Create a file named `.env` in the directory where you want the analysis to run. Add:

```
WINDSOR_API_KEY=your_actual_key_here
```

**The key is never accepted via command-line flag or chat paste.** Both vectors leak keys (shell history, `ps` output, agent transcripts). Local `.env` only.

### 2. Run the skill

In Claude Code, just say one of these:
- *"analyze my Instagram reels"*
- *"Instagram deep dive"*
- *"find my top performing reels"*
- *"what's working on my Instagram"*

Or invoke the orchestrator directly (it auto-loads `.env` from the working directory):

```bash
cd /path/to/directory/with/.env
python3 ~/.claude/skills/instagram-reels-analysis/scripts/run_analysis.py \
  --output-dir ./instagram-analysis
```

### Flags
| Flag | Default | Notes |
|------|---------|-------|
| `--output-dir` | *(required)* | Where to write artifacts |
| `--env-file` | `.env` in cwd | Path to the file containing `WINDSOR_API_KEY=...` |
| `--top-n` | `25` | How many reels to download + transcribe |
| `--whisper-model` | `small.en` | `tiny.en`/`base.en`/`small.en`/`medium.en` — tradeoff speed vs. quality |
| `--date-from` | auto (2 yrs back) | `YYYY-MM-DD` |
| `--date-to` | today | `YYYY-MM-DD` |

> `--api-key` is intentionally **not accepted**. The script exits with an error if you try — to protect your key from shell history and process listings.

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
