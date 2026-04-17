#!/usr/bin/env python3
"""
End-to-end Instagram Reels analysis pipeline.

Usage:
    python3 run_analysis.py --api-key KEY --output-dir DIR [--top-n 25]

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
    summary = {
        "account": ranked[0].get("account_name", "") if ranked else "",
        "total_reels": len(dashboard_data),
        "top_n": top_n_effective,
        "date_earliest": dates[0] if dates else "",
        "date_latest": dates[-1] if dates else "",
        "topN_avg_duration_s": avg(top_durs),
        "topN_median_duration_s": sorted(top_durs)[len(top_durs)//2] if top_durs else 0,
        "topN_avg_views": avg([r["views"] for r in top_cohort]),
        "topN_avg_comments": avg([r["comments"] for r in top_cohort]),
        "topN_avg_saves": avg([r["saves"] for r in top_cohort]),
        "topN_avg_shares": avg([r["shares"] for r in top_cohort]),
        "topN_avg_likes": avg([r["likes"] for r in top_cohort]),
        "all_avg_views": avg([r["views"] for r in dashboard_data]),
        "all_avg_comments": avg([r["comments"] for r in dashboard_data]),
        "all_avg_saves": avg([r["saves"] for r in dashboard_data]),
        "all_avg_shares": avg([r["shares"] for r in dashboard_data]),
        "all_avg_likes": avg([r["likes"] for r in dashboard_data]),
        "transcripts_complete": sum(1 for r in dashboard_data if r["transcript"]),
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--api-key", required=True, help="Windsor.ai Instagram connector API key")
    p.add_argument("--output-dir", required=True, help="Directory to write all artifacts")
    p.add_argument("--top-n", type=int, default=25, help="Number of reels to download + transcribe (default: 25)")
    p.add_argument("--whisper-model", default="small.en", help="Whisper model (tiny.en, base.en, small.en, medium.en)")
    p.add_argument("--date-from", default=None, help="Override start date (YYYY-MM-DD). Default: auto-detect ~2 years back")
    p.add_argument("--date-to", default=None, help="Override end date (YYYY-MM-DD). Default: today")
    args = p.parse_args()

    root = Path(args.output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)

    # Date range
    if args.date_from and args.date_to:
        date_from, date_to = args.date_from, args.date_to
    else:
        date_from, date_to = detect_account_date_range(args.api_key, root / "data")
        if args.date_from: date_from = args.date_from
        if args.date_to: date_to = args.date_to

    print(f"\n=== Instagram Reels Analysis ===")
    print(f"Output: {root}")
    print(f"Dates: {date_from} → {date_to}")
    print(f"Top-N: {args.top_n} · Whisper: {args.whisper_model}\n")

    # Step 1: media info
    media_info = fetch_media_info(args.api_key, root / "data", date_from, date_to)
    if not media_info:
        print("Failed to fetch media_info — aborting.")
        sys.exit(1)
    print(f"\nFound {len(media_info)} posts")

    # Step 2: insights (chunked)
    insights = fetch_insights_chunked(args.api_key, root / "data", date_from, date_to)

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
