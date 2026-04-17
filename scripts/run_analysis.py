#!/usr/bin/env python3
"""
End-to-end Instagram Reels analysis pipeline.

Usage:
    python3 run_analysis.py --output-dir DIR [--top-n 25]

The Windsor API key is NEVER passed on the command line or via chat. It is
loaded from a local .env file (or the WINDSOR_API_KEY environment variable).
Command-line passing is rejected to prevent the key leaking into shell history
or process lists.

Fetches reels from Windsor.ai, ranks by weighted composite score, downloads the
top N, transcribes them with OpenAI Whisper (local), and builds a CSV +
interactive HTML dashboard + written analysis.
"""
import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# ---------------- Windsor helpers ----------------

WINDSOR_BASE = "https://connectors.windsor.ai/instagram"

def curl_json(url, out_path, max_time=180, label=""):
    """Use curl (supports --max-time) to fetch a Windsor endpoint."""
    if label:
        print(f"  [{label}] fetching…", flush=True)
    t0 = time.time()
    result = subprocess.run([
        "curl", "-sS",
        "--connect-timeout", "15",
        "--max-time", str(max_time),
        url,
        "-o", str(out_path),
    ], capture_output=True, text=True)
    elapsed = time.time() - t0
    if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        print(f"  [{label}] FAILED after {elapsed:.1f}s: {result.stderr.strip()[:200]}")
        return None
    try:
        with open(out_path) as f:
            blob = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  [{label}] BAD JSON after {elapsed:.1f}s: {e}")
        return None
    rows = blob["data"] if isinstance(blob, dict) else blob
    print(f"  [{label}] {len(rows)} rows · {elapsed:.1f}s")
    return rows


def fetch_media_info(api_key, out_dir, date_from, date_to):
    """Pull media_info (captions, URLs, basic like/comment counts) for all posts."""
    fields = ",".join([
        "account_name", "media_id", "media_shortcode", "media_permalink",
        "media_caption", "media_type", "media_product_type",
        "media_url", "media_thumbnail_url", "timestamp",
        "media_like_count", "media_comments_count",
    ])
    url = f"{WINDSOR_BASE}?api_key={api_key}&fields={fields}&date_from={date_from}&date_to={date_to}"
    out = out_dir / "media_info.json"
    return curl_json(url, out, max_time=300, label=f"media_info {date_from}..{date_to}")


def fetch_insights_chunked(api_key, out_dir, date_from, date_to):
    """Pull media_insights in monthly chunks (Windsor hangs on wide joins).

    Returns a merged, deduplicated list of insight rows.
    """
    fields = ",".join([
        "media_id", "media_shortcode",
        "media_views", "media_saved", "media_shares",
        "media_reach", "media_reel_total_interactions", "media_reel_avg_watch_time",
    ])

    start = datetime.fromisoformat(date_from)
    end = datetime.fromisoformat(date_to)
    chunks_dir = out_dir / "chunks"
    chunks_dir.mkdir(exist_ok=True)

    # Generate ~2-week windows
    windows = []
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=14), end)
        windows.append((cur.date().isoformat(), nxt.date().isoformat()))
        cur = nxt + timedelta(days=1)

    print(f"\nFetching insights in {len(windows)} biweekly chunks…")
    all_rows = []
    seen = set()
    for i, (a, b) in enumerate(windows, 1):
        label = f"{a}..{b} ({i}/{len(windows)})"
        out = chunks_dir / f"insights_{a}_{b}.json"
        if out.exists() and out.stat().st_size > 10:
            try:
                with open(out) as f:
                    blob = json.load(f)
                rows = blob["data"] if isinstance(blob, dict) else blob
                print(f"  [{label}] cached · {len(rows)} rows")
            except Exception:
                rows = None
        else:
            rows = None
        if rows is None:
            url = f"{WINDSOR_BASE}?api_key={api_key}&fields={fields}&date_from={a}&date_to={b}"
            rows = curl_json(url, out, max_time=180, label=label)
            time.sleep(5)  # cooldown — Windsor rate-limits concurrent chunks
            if rows is None:
                continue
        for r in rows:
            key = r.get("media_id")
            if key and key not in seen:
                seen.add(key)
                all_rows.append(r)

    merged = out_dir / "media_insights.json"
    with open(merged, "w") as f:
        json.dump(all_rows, f)
    print(f"\nMerged insights: {len(all_rows)} unique reels")
    return all_rows


# ---------------- Ranking ----------------

def rank_reels(media_info, insights, out_dir, weights=None):
    """Merge + rank by weighted z-score.

    Default weights match the user's priority: comments > views > saves > shares > likes.
    """
    if weights is None:
        weights = {
            "media_comments_count": 0.30,
            "media_views":           0.25,
            "media_saved":           0.20,
            "media_shares":          0.15,
            "media_like_count":      0.10,
        }

    ins_map = {r["media_id"]: r for r in insights}
    rows = []
    for r in media_info:
        ins = ins_map.get(r["media_id"], {})
        merged = {**r, **ins}
        for k in ("media_like_count", "media_comments_count", "media_views",
                  "media_saved", "media_shares", "media_reach",
                  "media_reel_total_interactions", "media_reel_avg_watch_time"):
            v = merged.get(k)
            try:
                merged[k] = float(v) if v not in (None, "", "null") else 0.0
            except (TypeError, ValueError):
                merged[k] = 0.0
        merged["views_final"] = merged["media_views"] or 0
        rows.append(merged)

    # z-score normalize each weighted metric
    for m in weights:
        vals = [r.get(m, 0) or 0 for r in rows]
        mean = statistics.mean(vals) if vals else 0
        stdev = statistics.stdev(vals) if len(vals) > 1 and statistics.stdev(vals) > 0 else 1
        for r in rows:
            r[f"{m}_z"] = ((r.get(m, 0) or 0) - mean) / stdev

    for r in rows:
        r["composite_score"] = sum(r[f"{m}_z"] * w for m, w in weights.items())
    rows.sort(key=lambda r: r["composite_score"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    with open(out_dir / "reels_ranked.json", "w") as f:
        json.dump(rows, f, indent=2, default=str)
    return rows


# ---------------- Download + transcribe ----------------

def download_one(reel, videos_dir):
    url = reel.get("media_url")
    sc = reel.get("media_shortcode") or reel["media_id"]
    out = videos_dir / f"{sc}.mp4"
    if out.exists() and out.stat().st_size > 1000:
        return (sc, "cached", out.stat().st_size)
    if not url:
        return (sc, "no-url", 0)
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=90) as resp:
            data = resp.read()
        with open(out, "wb") as f:
            f.write(data)
        return (sc, "ok", len(data))
    except (HTTPError, URLError, Exception) as e:
        return (sc, f"err:{str(e)[:80]}", 0)


def ffprobe_duration(path):
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path)
        ], stderr=subprocess.DEVNULL).decode().strip()
        return float(out) if out else None
    except Exception:
        return None


def extract_audio(video_path, audio_dir):
    out = audio_dir / f"{video_path.stem}.mp3"
    if out.exists() and out.stat().st_size > 1000:
        return out
    subprocess.run([
        "ffmpeg", "-y", "-i", str(video_path), "-vn",
        "-ac", "1", "-ar", "16000",
        "-acodec", "libmp3lame", "-q:a", "4", str(out),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


def transcribe(audio_path, transcripts_dir, model="small.en"):
    out_json = transcripts_dir / f"{audio_path.stem}.json"
    if out_json.exists() and out_json.stat().st_size > 100:
        with open(out_json) as f:
            return json.load(f)
    subprocess.run([
        "whisper", str(audio_path),
        "--model", model,
        "--output_dir", str(transcripts_dir),
        "--output_format", "json",
        "--language", "en",
        "--fp16", "False",
        "--verbose", "False",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(out_json) as f:
        return json.load(f)


def download_and_transcribe(top_reels, root, model="small.en"):
    videos = root / "videos"
    audio = root / "audio"
    transcripts = root / "transcripts"
    for d in (videos, audio, transcripts):
        d.mkdir(exist_ok=True)

    print(f"\nDownloading {len(top_reels)} videos…")
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda r: download_one(r, videos), top_reels))
    for sc, status, size in results:
        print(f"  {sc}: {status} ({size:,} bytes)")

    ok_videos = [v for v in videos.glob("*.mp4") if v.stat().st_size > 1000]
    print(f"\nTranscribing {len(ok_videos)} videos with Whisper ({model})…")

    def process(video_path):
        try:
            a = extract_audio(video_path, audio)
            tx = transcribe(a, transcripts, model=model)
            return (video_path.stem, "ok", len(tx.get("text", "")))
        except Exception as e:
            return (video_path.stem, f"err:{str(e)[:80]}", 0)

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(process, v) for v in ok_videos]
        for fut in as_completed(futures):
            sc, status, tlen = fut.result()
            print(f"  {sc}: {status} · {tlen} chars")


# ---------------- Hook + insights analysis (dynamic) ----------------

_STOP_OPEN = {"the", "a", "an", "and", "or", "but", "so", "of", "for", "to", "in", "on", "at", "is", "are"}

def _normalize_word(w):
    """Lowercase, strip punct/possessives. Keep contractions readable."""
    w = re.sub(r"[^\w']", "", w.lower())
    return w


def compute_opening_phrases(top_cohort, n_shown=8):
    """Find the most frequent opening phrases across hooks in the top cohort.

    Groups hooks by their first 3 meaningful words (skipping leading articles),
    returns the top N by count plus one example hook for each.
    """
    from collections import Counter
    phrase_bucket = Counter()
    examples = {}
    for r in top_cohort:
        hook = (r.get("hook") or "").strip()
        if not hook or len(hook) < 8:
            continue
        # Take first ~6 tokens, strip leading stop words, keep first 3 meaningful words
        tokens = re.findall(r"[A-Za-z']+", hook)
        meaningful = []
        for t in tokens:
            nw = _normalize_word(t)
            if not meaningful and nw in _STOP_OPEN:
                continue
            meaningful.append(nw)
            if len(meaningful) == 3:
                break
        if len(meaningful) < 2:
            continue
        key = " ".join(meaningful)
        phrase_bucket[key] += 1
        examples.setdefault(key, hook)

    out = []
    for phrase, count in phrase_bucket.most_common(n_shown):
        if count < 2:
            break  # only surface recurring phrases (count >= 2)
        out.append({
            "phrase": phrase,
            "count": count,
            "example": examples[phrase][:180],
        })
    return out


def compute_structural_patterns(top_cohort):
    """Classify each hook into universal structural families and return counts.

    Families are non-exclusive — a hook can match multiple (e.g. a question
    that also directly addresses 'you'). Always relevant regardless of topic.
    """
    hooks = [(r.get("hook") or "").strip() for r in top_cohort if (r.get("hook") or "").strip()]
    total = len(hooks)
    if total == 0:
        return []

    def count(pred):
        return sum(1 for h in hooks if pred(h))

    def example(pred):
        for h in hooks:
            if pred(h):
                return h[:180]
        return ""

    patterns = [
        {
            "name": "Question opener",
            "desc": "ends with ? or starts with have/did/do/what/why/how",
            "pred": lambda h: h.rstrip().endswith("?") or bool(re.match(r"^(have|did|do|does|what|why|how|can|should)\b", h, re.I)),
        },
        {
            "name": "Direct address (\"you\")",
            "desc": "opens with you/your/you're",
            "pred": lambda h: bool(re.match(r"^(you['\u2019]?(re|ve|ll)?|your|if you)\b", h, re.I)),
        },
        {
            "name": "Listicle / numbered",
            "desc": "contains N + counted noun or 'here are N'",
            "pred": lambda h: bool(re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+(things|reasons|ways|tips|levels|signs|rules|steps|experts|videos|lectures|skills)\b", h, re.I)) or bool(re.match(r"^(here (are|\'s)|these|top \d)", h, re.I)),
        },
        {
            "name": "Data-backed claim",
            "desc": "contains a specific number (N%, N jobs, 1,700 posts…)",
            "pred": lambda h: bool(re.search(r"\d+\s?(%|percent|jobs|posts|roles|videos|reels|hours|cases|reports|interviews|people|creators)\b", h, re.I)),
        },
        {
            "name": "Imperative / command",
            "desc": "starts with a verb telling the viewer what to do",
            "pred": lambda h: bool(re.match(r"^(stop|start|use|copy|steal|watch|try|check|learn|build|follow|don'?t|never|always)\b", h, re.I)),
        },
        {
            "name": "News / novelty",
            "desc": "references a recent release, launch, or 'just announced'",
            "pred": lambda h: bool(re.search(r"\b(just (announced|dropped|launched|released)|can now|new|today|this week)\b", h, re.I)),
        },
        {
            "name": "Personal / first-person",
            "desc": "opens with I/my/we/our",
            "pred": lambda h: bool(re.match(r"^(i['\u2019]?m?|my|i\s|we|our)\b", h, re.I)),
        },
        {
            "name": "Contrarian framing",
            "desc": "most people / nobody / everyone / but actually",
            "pred": lambda h: bool(re.search(r"\b(most (people|of you)|everyone|nobody|no one|but actually|the truth is)\b", h, re.I)),
        },
    ]

    out = []
    for p in patterns:
        c = count(p["pred"])
        if c > 0:
            out.append({
                "name": p["name"],
                "desc": p["desc"],
                "count": c,
                "pct": round(100 * c / total, 1),
                "example": example(p["pred"]),
            })
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


def compute_insights(all_data, top_cohort, top_n, metrics, structural_patterns, opening_phrases):
    """Produce a structured strategic analysis: working, not working, double down, experiment.

    Each bullet is a dict with {headline, detail} — short, specific, data-backed.
    """
    from collections import Counter
    working, not_working, double_down, experiment = [], [], [], []

    # ---------- Metric multipliers: which engagement axis is distinctive? ----------
    ratios = {}
    for name, (top_v, all_v) in metrics.items():
        if all_v and all_v > 0:
            ratios[name] = top_v / all_v
    if ratios:
        top_metric, top_mult = max(ratios.items(), key=lambda kv: kv[1])
        low_metric, low_mult = min(ratios.items(), key=lambda kv: kv[1])
        working.append({
            "headline": f"{top_metric.capitalize()} is your sharpest signal",
            "detail": f"Top-{top_n} average {top_metric} are {top_mult:.1f}× baseline — the widest gap of any metric. Whatever you're doing here, keep it.",
        })
        if top_mult / max(low_mult, 0.01) > 2 and low_mult < 2:
            not_working.append({
                "headline": f"{low_metric.capitalize()} lags the rest",
                "detail": f"Your top reels outperform on every metric, but {low_metric} only reach {low_mult:.1f}× baseline vs {top_mult:.1f}× on {top_metric}. The {low_metric} axis is undertuned.",
            })

    # ---------- Duration analysis ----------
    durs = [r.get("duration_s") for r in top_cohort if r.get("duration_s")]
    if durs:
        buckets = {"<30s": 0, "30-45s": 0, "45-60s": 0, "60-90s": 0, ">90s": 0}
        for d in durs:
            if d < 30: buckets["<30s"] += 1
            elif d < 45: buckets["30-45s"] += 1
            elif d < 60: buckets["45-60s"] += 1
            elif d < 90: buckets["60-90s"] += 1
            else: buckets[">90s"] += 1
        dominant = max(buckets.items(), key=lambda kv: kv[1])
        if dominant[1] >= max(2, len(durs) // 3):
            working.append({
                "headline": f"{dominant[0]} is your sweet spot",
                "detail": f"{dominant[1]} of your top {len(durs)} transcribed reels are in this duration band. Shorter cuts over-index per-second; longer ones rarely break through.",
            })
        weak = [k for k, v in buckets.items() if v == 0]
        if ">90s" in weak or "60-90s" in weak:
            not_working.append({
                "headline": "Long reels rarely break through",
                "detail": "Almost none of your top performers exceed 60 seconds. Tighten scripts; every second of runtime past ~55s is fighting attention decay.",
            })

    # ---------- CTA analysis ----------
    cta_re = re.compile(r"comment\s+[\"']?\w+[\"']?", re.I)
    has_cta = [r for r in top_cohort if cta_re.search((r.get("caption") or "") + " " + (r.get("cta_tail") or ""))]
    no_cta = [r for r in top_cohort if not cta_re.search((r.get("caption") or "") + " " + (r.get("cta_tail") or ""))]
    if has_cta and no_cta:
        avg_with = sum(r["comments"] for r in has_cta) / len(has_cta)
        avg_without = sum(r["comments"] for r in no_cta) / len(no_cta)
        if avg_with > avg_without * 1.3:
            working.append({
                "headline": "\"Comment [keyword]\" CTAs are doing heavy lifting",
                "detail": f"{len(has_cta)} of your top {len(top_cohort)} reels use a comment-keyword CTA. Those average {avg_with:,.0f} comments vs {avg_without:,.0f} on reels without one ({avg_with/max(avg_without,1):.1f}×).",
            })
            if len(no_cta) >= 3:
                double_down.append({
                    "headline": "Add a comment CTA to every reel",
                    "detail": f"{len(no_cta)} of your top reels don't use one and still made the cut — but they under-index on comments. A distinctive keyword per lecture unlocks DM-funnel tracking.",
                })

    # ---------- Structural pattern winners ----------
    if structural_patterns:
        strong = [p for p in structural_patterns if p["pct"] >= 30]
        weak = [p for p in structural_patterns if p["pct"] <= 10]
        if strong:
            top_p = strong[0]
            working.append({
                "headline": f"\"{top_p['name']}\" hooks dominate",
                "detail": f"{top_p['pct']}% of your top hooks use this pattern — {top_p['desc']}. It's the format your audience has voted for.",
            })
        if weak:
            # Pattern that IS present in some top reels but underused
            candidates = [p for p in structural_patterns if 5 <= p["pct"] <= 20]
            if candidates:
                c = candidates[0]
                experiment.append({
                    "headline": f"Test more \"{c['name']}\" hooks",
                    "detail": f"Only {c['pct']}% of your top reels use this format — {c['desc']} — but it's working when you do. Worth 2-3 test reels to see if you can scale it.",
                })

    # ---------- Opening phrase reuse ----------
    if opening_phrases and opening_phrases[0]["count"] >= 3:
        top_phrase = opening_phrases[0]
        double_down.append({
            "headline": f"You've codified a winning opener: \"{top_phrase['phrase']}…\"",
            "detail": f"This phrase appears in {top_phrase['count']} of your top hooks. Script variations of it into your backlog — it's a proven pattern.",
        })

    # ---------- Posting cadence / recency ----------
    dates = sorted([r["timestamp"] for r in all_data if r.get("timestamp")])
    top_dates = sorted([r["timestamp"] for r in top_cohort if r.get("timestamp")])
    if len(dates) >= 10 and top_dates:
        mid = dates[len(dates) // 2]
        recent_top = sum(1 for d in top_dates if d >= mid)
        if recent_top / max(len(top_dates), 1) > 0.65:
            working.append({
                "headline": "Your recent reels are winning more",
                "detail": f"{recent_top} of your top {len(top_dates)} reels were posted in the second half of the date range — whatever you've been tuning recently, it's working.",
            })
        elif recent_top / max(len(top_dates), 1) < 0.35:
            not_working.append({
                "headline": "Recent reels under-represent in the top",
                "detail": f"Only {recent_top} of your top {len(top_dates)} reels were posted in the second half of the date range — something in the recent playbook has slipped.",
            })

    # ---------- Caption keyword concentration ----------
    cap_tokens = Counter()
    for r in top_cohort:
        for w in re.findall(r"[A-Za-z]{3,}", (r.get("caption") or "").lower()):
            if w in _STOP_OPEN or len(w) < 3: continue
            cap_tokens[w] += 1
    all_tokens = Counter()
    for r in all_data:
        for w in re.findall(r"[A-Za-z]{3,}", (r.get("caption") or "").lower()):
            if w in _STOP_OPEN or len(w) < 3: continue
            all_tokens[w] += 1
    for kw, top_count in cap_tokens.most_common(6):
        if top_count < 4:
            continue
        all_count = all_tokens.get(kw, 0)
        if all_count == 0:
            continue
        top_rate = top_count / max(len(top_cohort), 1)
        all_rate = all_count / max(len(all_data), 1)
        if top_rate > all_rate * 1.5 and top_rate > 0.15:
            double_down.append({
                "headline": f"Topic \"{kw}\" is a runaway winner",
                "detail": f"\"{kw}\" appears in {top_count}/{len(top_cohort)} top reels vs {all_count}/{len(all_data)} overall. It's {top_rate/all_rate:.1f}× over-represented in your top.",
            })
            break  # one caption winner is enough

    return {
        "working": working[:4],
        "not_working": not_working[:3],
        "double_down": double_down[:3],
        "experiment": experiment[:3],
    }


# ---------------- Report builder ----------------

def build_report(ranked, root, top_n=25):
    videos = root / "videos"
    transcripts = root / "transcripts"
    output = root / "output"
    output.mkdir(exist_ok=True)

    def load_transcript(sc):
        p = transcripts / f"{sc}.json"
        if p.exists() and p.stat().st_size > 100:
            with open(p) as f:
                return json.load(f).get("text", "").strip()
        return None

    for r in ranked:
        sc = r["media_shortcode"]
        vp = videos / f"{sc}.mp4"
        r["duration_s"] = ffprobe_duration(vp) if vp.exists() else None
        r["transcript"] = load_transcript(sc)
        if r["transcript"]:
            sents = re.split(r'(?<=[.!?])\s+', r["transcript"])
            r["hook"] = " ".join(sents[:2])[:240]
            words = r["transcript"].split()
            r["cta_tail"] = " ".join(words[-35:])
        else:
            r["hook"] = ""
            r["cta_tail"] = ""

    # CSV
    import csv
    csv_cols = [
        "rank", "media_shortcode", "media_permalink", "timestamp", "media_type",
        "duration_s", "media_like_count", "media_comments_count",
        "views_final", "media_saved", "media_shares", "media_reach",
        "media_reel_total_interactions", "composite_score",
        "media_caption", "hook", "cta_tail", "transcript",
    ]
    with open(output / "reels_analysis.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=csv_cols, extrasaction="ignore")
        w.writeheader()
        for r in ranked:
            row = {c: r.get(c, "") for c in csv_cols}
            for k in ("media_caption", "hook", "cta_tail", "transcript"):
                if row.get(k):
                    row[k] = re.sub(r'\s+', ' ', str(row[k])).strip()
            w.writerow(row)

    # Dashboard JSON
    dashboard_data = []
    for r in ranked:
        dashboard_data.append({
            "rank": r["rank"],
            "shortcode": r["media_shortcode"],
            "permalink": r.get("media_permalink", ""),
            "timestamp": (r.get("timestamp") or "")[:10],
            "duration_s": r.get("duration_s"),
            "likes": r.get("media_like_count", 0),
            "comments": r.get("media_comments_count", 0),
            "views": r.get("views_final", 0),
            "saves": r.get("media_saved", 0),
            "shares": r.get("media_shares", 0),
            "reach": r.get("media_reach", 0),
            "score": round(r.get("composite_score", 0), 3),
            "caption": (r.get("media_caption") or "")[:600],
            "hook": r.get("hook", ""),
            "cta_tail": r.get("cta_tail", ""),
            "transcript": r.get("transcript", ""),
        })
    with open(output / "reels_data.json", "w") as f:
        json.dump(dashboard_data, f)

    # Summary
    def avg(xs):
        xs = [x for x in xs if x]
        return sum(xs) / len(xs) if xs else 0

    top_n_effective = min(top_n, len(dashboard_data))
    top_cohort = dashboard_data[:top_n_effective]
    top_durs = [r["duration_s"] for r in top_cohort if r["duration_s"]]
    dates = sorted([r["timestamp"] for r in dashboard_data if r.get("timestamp")])

    opening_phrases = compute_opening_phrases(top_cohort)
    structural_patterns = compute_structural_patterns(top_cohort)
    topN_avg_views = avg([r["views"] for r in top_cohort])
    topN_avg_comments = avg([r["comments"] for r in top_cohort])
    topN_avg_saves = avg([r["saves"] for r in top_cohort])
    topN_avg_shares = avg([r["shares"] for r in top_cohort])
    topN_avg_likes = avg([r["likes"] for r in top_cohort])
    all_avg_views = avg([r["views"] for r in dashboard_data])
    all_avg_comments = avg([r["comments"] for r in dashboard_data])
    all_avg_saves = avg([r["saves"] for r in dashboard_data])
    all_avg_shares = avg([r["shares"] for r in dashboard_data])
    all_avg_likes = avg([r["likes"] for r in dashboard_data])

    insights = compute_insights(
        dashboard_data, top_cohort, top_n_effective,
        metrics={
            "views": (topN_avg_views, all_avg_views),
            "comments": (topN_avg_comments, all_avg_comments),
            "saves": (topN_avg_saves, all_avg_saves),
            "shares": (topN_avg_shares, all_avg_shares),
            "likes": (topN_avg_likes, all_avg_likes),
        },
        structural_patterns=structural_patterns,
        opening_phrases=opening_phrases,
    )

    summary = {
        "account": ranked[0].get("account_name", "") if ranked else "",
        "total_reels": len(dashboard_data),
        "top_n": top_n_effective,
        "date_earliest": dates[0] if dates else "",
        "date_latest": dates[-1] if dates else "",
        "topN_avg_duration_s": avg(top_durs),
        "topN_median_duration_s": sorted(top_durs)[len(top_durs)//2] if top_durs else 0,
        "topN_avg_views": topN_avg_views,
        "topN_avg_comments": topN_avg_comments,
        "topN_avg_saves": topN_avg_saves,
        "topN_avg_shares": topN_avg_shares,
        "topN_avg_likes": topN_avg_likes,
        "all_avg_views": all_avg_views,
        "all_avg_comments": all_avg_comments,
        "all_avg_saves": all_avg_saves,
        "all_avg_shares": all_avg_shares,
        "all_avg_likes": all_avg_likes,
        "transcripts_complete": sum(1 for r in dashboard_data if r["transcript"]),
        "opening_phrases": opening_phrases,
        "structural_patterns": structural_patterns,
        "insights": insights,
    }
    with open(output / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    # Inject into HTML template
    skill_dir = Path(__file__).resolve().parent.parent
    template = skill_dir / "templates" / "dashboard_template.html"
    with open(template) as f:
        html = f.read()
    html = html.replace("__DATA__", json.dumps(dashboard_data))
    html = html.replace("__SUMMARY__", json.dumps(summary))
    handle = summary.get("account") or "account"
    display_handle = f"@{handle}" if not handle.startswith("@") else handle
    html = html.replace("__ACCOUNT__", display_handle)
    with open(output / "dashboard.html", "w") as f:
        f.write(html)

    print(f"\nOutputs written to {output}/")
    print(f"  dashboard.html · {len(html):,} chars")
    print(f"  reels_analysis.csv · {len(dashboard_data)} rows")
    print(f"  summary.json · account @{handle}, {len(dashboard_data)} posts, {summary['transcripts_complete']} transcripts")
    return output / "dashboard.html"


# ---------------- Main ----------------

def detect_account_date_range(api_key, out_dir):
    """Detect earliest and latest post dates by asking Windsor for a small field."""
    # Try last_7d first to confirm the API key is valid
    url = f"{WINDSOR_BASE}?api_key={api_key}&fields=media_shortcode,timestamp&date_preset=last_7d"
    out = out_dir / "_probe.json"
    rows = curl_json(url, out, max_time=60, label="API probe")
    if rows is None:
        raise RuntimeError("Windsor API probe failed — check your api key")
    # Probe further back to find actual start date
    today = datetime.utcnow().date()
    two_years_ago = (today - timedelta(days=730)).isoformat()
    return two_years_ago, today.isoformat()


def _load_dotenv(path):
    """Minimal .env parser: KEY=VALUE, '#' for comments. Returns dict."""
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def _resolve_api_key(env_file):
    """Load WINDSOR_API_KEY from .env (preferred), then fall back to process env.

    Never accepts the key on the command line — that would leak it into shell
    history and `ps` output. Never echoes the key in error messages.
    """
    env_path = Path(env_file).expanduser().resolve()
    env = _load_dotenv(env_path)
    key = env.get("WINDSOR_API_KEY") or os.environ.get("WINDSOR_API_KEY", "").strip()
    if not key:
        print("\nERROR: WINDSOR_API_KEY not found.\n")
        print("This tool reads your Windsor.ai key from a .env file to keep it out of shell")
        print("history, process lists, and Claude conversation logs.\n")
        print("To fix:")
        print(f"  1. Create {env_path}")
        print(f"  2. Add this line:  WINDSOR_API_KEY=your_key_here")
        print(f"  3. Re-run this script.\n")
        print("Get a key at https://windsor.ai (Instagram connector).")
        sys.exit(2)
    # Sanity-check: obvious pasted-with-quotes or space issues
    if any(c.isspace() for c in key):
        print("\nERROR: WINDSOR_API_KEY contains whitespace — check your .env for stray spaces.")
        sys.exit(2)
    return key


def main():
    p = argparse.ArgumentParser(
        description="Instagram Reels analysis — loads WINDSOR_API_KEY from .env",
    )
    p.add_argument("--output-dir", required=True, help="Directory to write all artifacts")
    p.add_argument("--env-file", default=".env",
                   help="Path to .env file containing WINDSOR_API_KEY (default: ./.env)")
    p.add_argument("--top-n", type=int, default=25, help="Number of reels to download + transcribe (default: 25)")
    p.add_argument("--whisper-model", default="small.en", help="Whisper model (tiny.en, base.en, small.en, medium.en)")
    p.add_argument("--date-from", default=None, help="Override start date (YYYY-MM-DD). Default: auto-detect ~2 years back")
    p.add_argument("--date-to", default=None, help="Override end date (YYYY-MM-DD). Default: today")
    args = p.parse_args()

    # Explicitly reject --api-key to prevent accidental CLI leaks
    if any(a.startswith("--api-key") for a in sys.argv[1:]):
        print("ERROR: --api-key is not accepted on the command line (prevents shell-history leaks).")
        print("Put WINDSOR_API_KEY in a .env file instead. See --help.")
        sys.exit(2)

    api_key = _resolve_api_key(args.env_file)

    root = Path(args.output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)

    # Date range
    if args.date_from and args.date_to:
        date_from, date_to = args.date_from, args.date_to
    else:
        date_from, date_to = detect_account_date_range(api_key, root / "data")
        if args.date_from: date_from = args.date_from
        if args.date_to: date_to = args.date_to

    print(f"\n=== Instagram Reels Analysis ===")
    print(f"Output: {root}")
    print(f"Dates: {date_from} → {date_to}")
    print(f"Top-N: {args.top_n} · Whisper: {args.whisper_model}\n")

    # Step 1: media info
    media_info = fetch_media_info(api_key, root / "data", date_from, date_to)
    if not media_info:
        print("Failed to fetch media_info — aborting.")
        sys.exit(1)
    print(f"\nFound {len(media_info)} posts")

    # Step 2: insights (chunked)
    insights = fetch_insights_chunked(api_key, root / "data", date_from, date_to)

    # Step 3: rank
    ranked = rank_reels(media_info, insights, root / "data")
    print(f"\nRanked {len(ranked)} posts. Top 5:")
    for r in ranked[:5]:
        print(f"  #{r['rank']} {r['media_shortcode']} · views {int(r['views_final']):,} · "
              f"comm {int(r['media_comments_count']):,} · saves {int(r['media_saved']):,}")

    # Step 4: filter to reels only, take top N
    reel_ranked = [r for r in ranked
                   if r.get("media_type") in ("VIDEO", "REELS", "REEL")
                   or r.get("media_product_type") == "REELS"]
    top_reels = reel_ranked[:args.top_n]

    # Step 5: download + transcribe
    download_and_transcribe(top_reels, root, model=args.whisper_model)

    # Step 6: build report (uses ALL ranked, but only top-N have transcripts)
    dashboard_path = build_report(ranked, root, top_n=args.top_n)

    # Step 7: open
    if sys.platform == "darwin":
        subprocess.run(["open", str(dashboard_path)])
    elif sys.platform == "linux":
        subprocess.run(["xdg-open", str(dashboard_path)])
    elif sys.platform == "win32":
        os.startfile(str(dashboard_path))

    print(f"\n✓ Dashboard opened: {dashboard_path}")


if __name__ == "__main__":
    main()
