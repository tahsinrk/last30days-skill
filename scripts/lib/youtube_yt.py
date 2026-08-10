"""YouTube search and transcript extraction via yt-dlp for /last30days v2.1.

Uses yt-dlp (https://github.com/yt-dlp/yt-dlp) for both YouTube search and
transcript extraction. No API keys needed — just have yt-dlp installed.

Inspired by Peter Steinberger's toolchain approach (yt-dlp + summarize CLI).
"""

import json
import math
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Python 3.11+ supports process_group kwarg (fork-safe alternative to preexec_fn).
_PY311_PLUS = sys.version_info >= (3, 11)

# Depth configurations: how many videos to search / transcribe
DEPTH_CONFIG = {
    "quick": 10,
    "default": 20,
    "deep": 40,
}

TRANSCRIPT_LIMITS = {
    "quick": 3,
    "default": 5,
    "deep": 8,
}

# Max words to keep from each transcript
TRANSCRIPT_MAX_WORDS = 5000

# Subtitle languages to request, in preference order. English-only silently
# dropped every non-English video even when it had perfectly good captions.
# Override with LAST30DAYS_YT_SUB_LANGS="en,de" etc.
_DEFAULT_SUB_LANGS = "en,es,pt"

# Transcript-fetch resilience. yt-dlp exits 0 with no file when a video
# genuinely has no captions, so a NON-zero exit means an actual fetch error
# (rate limit, bot gate, network) and is worth retrying. Without this the
# common 429 looked identical to "no captions" and the transcript was dropped.
_TRANSCRIPT_MAX_RETRIES = 2
_TRANSCRIPT_BACKOFF_BASE = 2.0  # seconds, multiplied by (attempt + 1)
_TRANSIENT_RE = re.compile(
    r"429|too many requests|sign in to confirm|not a bot|rate.?limit"
    r"|temporarily|try again|timed out|timeout|connection"
    r"|unable to (extract|download)|failed to (extract|download)|read error",
    re.IGNORECASE,
)

# How many extra videos to try transcripts for, as a multiple of the target.
# Music videos and short clips often lack captions, so attempting exactly the
# target number reliably returns fewer transcripts than asked for.
_TRANSCRIPT_OVERFETCH = 3

from . import dates
from .relevance import token_overlap_relevance as _compute_relevance


def _ytdlp_sub_langs() -> str:
    """Comma-separated subtitle languages to request from yt-dlp."""
    return os.environ.get("LAST30DAYS_YT_SUB_LANGS", "").strip() or _DEFAULT_SUB_LANGS


def _sub_lang_attempts() -> List[str]:
    """Subtitle language sets to try, in order, as separate yt-dlp runs.

    Passing "en,es,pt" in one call is tempting but costs an extra download per
    video: YouTube auto-translates, so an English video returns both en.vtt and
    es.vtt. Measured on 2026-08-10 — 2 files instead of 1. Every extra request
    feeds the rate limiting that makes transcripts fail in the first place.

    So ask for the primary language alone, and only reach for the fallbacks on
    videos where the primary genuinely produced nothing.
    """
    langs = [lang.strip() for lang in _ytdlp_sub_langs().split(",") if lang.strip()]
    if not langs:
        return ["en"]
    if len(langs) == 1:
        return langs
    return [langs[0], ",".join(langs[1:])]


def _transcript_backoff(video_id: str, attempt: int) -> float:
    """Seconds to wait before a transcript retry.

    Staggered by a sub-second offset derived from the video id so parallel
    workers don't all retry on the same tick and re-trip YouTube's limiter.
    """
    offset = (sum(ord(c) for c in video_id) % 1000) / 1000.0
    return _TRANSCRIPT_BACKOFF_BASE * (attempt + 1) + offset


def _read_vtt(video_id: str, temp_dir: str) -> Optional[str]:
    """Return whatever VTT yt-dlp wrote for this video, or None if there is none."""
    # yt-dlp may save as .en.vtt, .en-orig.vtt, .es.vtt, ...
    vtt_path = Path(temp_dir) / f"{video_id}.en.vtt"
    if not vtt_path.exists():
        for p in sorted(Path(temp_dir).glob(f"{video_id}*.vtt")):
            vtt_path = p
            break
        else:
            return None
    try:
        return vtt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _select_transcript_candidates(items: List[Dict[str, Any]], transcript_limit: int) -> List[str]:
    """Pick which videos to attempt transcripts for.

    Two problems with taking the first N in search order. Videos without
    captions burn the budget and leave you short, so overfetch. And an old
    high-view video beats a recent one on views alone, then gets pruned
    downstream for being outside the date window, wasting the slot. Rank on
    views and recency together.
    """
    if transcript_limit <= 0 or not items:
        return []

    def sort_key(item: Dict[str, Any]) -> tuple:
        engagement = item.get("engagement") or {}
        views = engagement.get("views") or 0
        recency = dates.recency_score(item.get("date") or "")
        return (recency, views)

    ranked = sorted(items, key=sort_key, reverse=True)
    attempt_count = min(len(ranked), transcript_limit * _TRANSCRIPT_OVERFETCH)
    return [item["video_id"] for item in ranked[:attempt_count]]


def extract_transcript_highlights(transcript: str, topic: str, limit: int = 5) -> List[str]:
    """Extract quotable highlights from a YouTube transcript.

    Similar to reddit_enrich.extract_comment_insights() but for
    continuous speech-to-text rather than threaded comments.
    """
    if not transcript:
        return []

    sentences = re.split(r'(?<=[.!?])\s+', transcript)

    filler = [
        r"^(hey |hi |what's up|welcome back|in today's video|don't forget to)",
        r"(subscribe|like and comment|hit the bell|check out the link|down below)",
        r"^(so |and |but |okay |alright |um |uh )",
        r"(thanks for watching|see you (next|in the)|bye)",
    ]

    topic_words = [w.lower() for w in topic.lower().split() if len(w) > 2]

    candidates = []
    for sent in sentences:
        sent = sent.strip()
        words = sent.split()
        if len(words) < 8 or len(words) > 50:
            continue
        if any(re.search(p, sent, re.IGNORECASE) for p in filler):
            continue

        score = 0
        if re.search(r'\d', sent):
            score += 2
        if re.search(r'[A-Z][a-z]+', sent):
            score += 1
        if '?' in sent:
            score += 1
        sent_lower = sent.lower()
        if any(w in sent_lower for w in topic_words):
            score += 2

        candidates.append((score, sent))

    candidates.sort(key=lambda x: -x[0])
    return [sent for _, sent in candidates[:limit]]


def _log(msg: str):
    """Log to stderr."""
    sys.stderr.write(f"[YouTube] {msg}\n")
    sys.stderr.flush()


def is_ytdlp_installed() -> bool:
    """Check if yt-dlp is available in PATH."""
    return shutil.which("yt-dlp") is not None


def _extract_core_subject(topic: str) -> str:
    """Extract core subject from verbose query for YouTube search.

    NOTE: 'tips', 'tricks', 'tutorial', 'guide', 'review', 'reviews'
    are intentionally KEPT — they're YouTube content types that improve search.
    """
    from .query import extract_core_subject
    # YouTube-specific noise set: smaller than default, keeps content-type words
    _YT_NOISE = frozenset({
        'best', 'top', 'good', 'great', 'awesome', 'killer',
        'latest', 'new', 'news', 'update', 'updates',
        'trending', 'hottest', 'popular', 'viral',
        'practices', 'features',
        'recommendations', 'advice',
        'prompt', 'prompts', 'prompting',
        'methods', 'strategies', 'approaches',
    })
    return extract_core_subject(topic, noise=_YT_NOISE)


def search_youtube(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> Dict[str, Any]:
    """Search YouTube via yt-dlp. No API key needed.

    Args:
        topic: Search topic
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        depth: 'quick', 'default', or 'deep'

    Returns:
        Dict with 'items' list of video metadata dicts.
    """
    if not is_ytdlp_installed():
        return {"items": [], "error": "yt-dlp not installed"}

    count = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    core_topic = _extract_core_subject(topic)

    _log(f"Searching YouTube for '{core_topic}' (since {from_date}, count={count})")

    # yt-dlp search with full metadata (no --flat-playlist so dates are real).
    # No --dateafter — we filter by date in Python with a soft fallback,
    # because YouTube search returns relevance-sorted results and strict date
    # filtering returns 0 for evergreen topics like "thumbnail tips".
    # The -- separator prevents topic strings starting with "-" from being
    # interpreted as flags.
    # --ignore-config prevents user's yt-dlp config from overriding security flags.
    cmd = [
        "yt-dlp",
        "--ignore-config",
        "--no-cookies-from-browser",
        "--dump-json",
        "--no-warnings",
        "--no-download",
        "--",
        f"ytsearch{count}:{core_topic}",
    ]

    # Python 3.11+ process_group is fork-safe; preexec_fn can deadlock with threads.
    pg_kwargs: Dict[str, Any] = {}
    if _PY311_PLUS:
        pg_kwargs["process_group"] = 0
    elif hasattr(os, "setsid"):
        pg_kwargs["preexec_fn"] = os.setsid

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **pg_kwargs,
        )
        try:
            stdout, stderr = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.wait(timeout=5)
            _log("YouTube search timed out (120s)")
            return {"items": [], "error": "Search timed out"}
    except FileNotFoundError:
        return {"items": [], "error": "yt-dlp not found"}

    if not (stdout or "").strip():
        _log("YouTube search returned 0 results")
        return {"items": []}

    # Parse JSON-per-line output
    items = []
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            video = json.loads(line)
        except json.JSONDecodeError:
            continue

        video_id = video.get("id", "")
        view_count = video.get("view_count") or 0
        like_count = video.get("like_count") or 0
        comment_count = video.get("comment_count") or 0
        upload_date = video.get("upload_date", "")  # YYYYMMDD

        # Convert YYYYMMDD to YYYY-MM-DD
        date_str = None
        if upload_date and len(upload_date) == 8:
            date_str = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

        items.append({
            "video_id": video_id,
            "title": video.get("title", ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "channel_name": video.get("channel", video.get("uploader", "")),
            "date": date_str,
            "engagement": {
                "views": view_count,
                "likes": like_count,
                "comments": comment_count,
            },
            "duration": video.get("duration"),
            "relevance": _compute_relevance(core_topic, video.get("title", "")),
            "why_relevant": f"YouTube: {video.get('title', core_topic)[:60]}",
        })

    # Soft date filter: prefer recent items but fall back to all if too few
    recent = [i for i in items if i["date"] and i["date"] >= from_date]
    if len(recent) >= 3:
        items = recent
        _log(f"Found {len(items)} videos within date range")
    else:
        _log(f"Found {len(items)} videos ({len(recent)} within date range, keeping all)")

    # Sort by views descending
    items.sort(key=lambda x: x["engagement"]["views"], reverse=True)

    return {"items": items}


def _clean_vtt(vtt_text: str) -> str:
    """Convert VTT subtitle format to clean plaintext."""
    # Strip VTT header
    text = re.sub(r'^WEBVTT.*?\n\n', '', vtt_text, flags=re.DOTALL)
    # Strip timestamps
    text = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}.*\n', '', text)
    # Strip position/alignment tags
    text = re.sub(r'<[^>]+>', '', text)
    # Strip cue numbers
    text = re.sub(r'^\d+\s*$', '', text, flags=re.MULTILINE)
    # Deduplicate overlapping lines
    lines = text.strip().split('\n')
    seen = set()
    unique = []
    for line in lines:
        stripped = line.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            unique.append(stripped)
    return re.sub(r'\s+', ' ', ' '.join(unique)).strip()


def fetch_transcript(video_id: str, temp_dir: str) -> Optional[str]:
    """Fetch auto-generated transcript for a YouTube video.

    Args:
        video_id: YouTube video ID
        temp_dir: Temporary directory for subtitle files

    Returns:
        Plaintext transcript string, or None if no captions available.
    """
    pg_kwargs: Dict[str, Any] = {}
    if _PY311_PLUS:
        pg_kwargs["process_group"] = 0
    elif hasattr(os, "setsid"):
        pg_kwargs["preexec_fn"] = os.setsid

    def run_once(sub_lang: str) -> Tuple[Optional[int], str]:
        """Run yt-dlp for one language set. Returns (returncode, stderr)."""
        cmd = [
            "yt-dlp",
            "--ignore-config",
            "--no-cookies-from-browser",
            "--write-auto-subs",
            "--sub-lang", sub_lang,
            "--sub-format", "vtt",
            "--skip-download",
            "--no-warnings",
            "-o", f"{temp_dir}/%(id)s",
            "--",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **pg_kwargs,
        )
        try:
            _, stderr = proc.communicate(timeout=30)
            return proc.returncode, stderr or ""
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.wait(timeout=5)
            return 1, "timed out"

    raw = None
    for sub_lang in _sub_lang_attempts():
        for attempt in range(_TRANSCRIPT_MAX_RETRIES + 1):
            try:
                returncode, stderr = run_once(sub_lang)
            except FileNotFoundError:
                # yt-dlp isn't installed; retrying cannot help.
                return None

            # Check for output before judging the exit code. yt-dlp can write a
            # complete track and still exit non-zero (a later language 429s, or
            # a benign teardown error). A partial success is a real transcript.
            raw = _read_vtt(video_id, temp_dir)
            if raw is not None:
                break

            # Exit 0 with no file means there are no captions in this language
            # set. Retrying the same request is pointless; fall through to the
            # next language set instead.
            if returncode == 0:
                break

            stderr = stderr.strip()
            if not _TRANSIENT_RE.search(stderr):
                last_line = stderr.splitlines()[-1][:200] if stderr else f"exit {returncode}"
                _log(f"Transcript fetch failed for {video_id}: {last_line}")
                return None

            if attempt < _TRANSCRIPT_MAX_RETRIES:
                delay = _transcript_backoff(video_id, attempt)
                _log(f"Transcript fetch for {video_id} hit a transient error; "
                     f"retrying in {delay:.1f}s")
                time.sleep(delay)
            else:
                # Rate limited even after backoff. Another language set would
                # hit the same limiter, so stop here rather than pile on.
                _log(f"Transcript fetch for {video_id} gave up after "
                     f"{_TRANSCRIPT_MAX_RETRIES + 1} attempts")
                return None

        if raw is not None:
            break

    if raw is None:
        return None

    transcript = _clean_vtt(raw)

    # Truncate to max words
    words = transcript.split()
    if len(words) > TRANSCRIPT_MAX_WORDS:
        transcript = ' '.join(words[:TRANSCRIPT_MAX_WORDS]) + '...'

    return transcript if transcript else None


def fetch_transcripts_parallel(
    video_ids: List[str],
    max_workers: int = 5,
) -> Dict[str, Optional[str]]:
    """Fetch transcripts for multiple videos in parallel.

    Args:
        video_ids: List of YouTube video IDs
        max_workers: Max parallel fetches

    Returns:
        Dict mapping video_id to transcript text (or None).
    """
    if not video_ids:
        return {}

    _log(f"Fetching transcripts for {len(video_ids)} videos")

    results = {}
    with tempfile.TemporaryDirectory() as temp_dir:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(fetch_transcript, vid, temp_dir): vid
                for vid in video_ids
            }
            for future in as_completed(futures):
                vid = futures[future]
                try:
                    results[vid] = future.result()
                except Exception:
                    results[vid] = None

    got = sum(1 for v in results.values() if v)
    _log(f"Got transcripts for {got}/{len(video_ids)} videos")
    return results


def search_and_transcribe(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> Dict[str, Any]:
    """Full YouTube search: find videos, then fetch transcripts for top results.

    Args:
        topic: Search topic
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        depth: 'quick', 'default', or 'deep'

    Returns:
        Dict with 'items' list. Each item has a 'transcript_snippet' field.
    """
    # Step 1: Search
    search_result = search_youtube(topic, from_date, to_date, depth)
    items = search_result.get("items", [])

    if not items:
        return search_result

    # Step 2: Fetch transcripts, ranked by recency and views.
    #
    # Videos without captions used to burn the budget: asking for exactly N
    # candidates returned fewer than N transcripts. Fetching 3N up front fixes
    # that but triples the yt-dlp calls, and YouTube rate-limiting is the main
    # reason transcripts fail in the first place. So top up in waves instead:
    # request the budget, and only reach for more candidates if some came back
    # empty. Happy path costs exactly N calls; a captionless-heavy result set
    # spends more only where it actually needs to.
    transcript_limit = TRANSCRIPT_LIMITS.get(depth, TRANSCRIPT_LIMITS["default"])
    candidate_ids = _select_transcript_candidates(items, transcript_limit)

    transcripts: Dict[str, Optional[str]] = {}
    pending = list(candidate_ids)
    while pending and sum(1 for v in transcripts.values() if v) < transcript_limit:
        needed = transcript_limit - sum(1 for v in transcripts.values() if v)
        wave, pending = pending[:needed], pending[needed:]
        transcripts.update(fetch_transcripts_parallel(wave))

    # Step 3: Attach transcripts and extract highlights
    core_topic = _extract_core_subject(topic)
    for item in items:
        vid = item["video_id"]
        transcript = transcripts.get(vid)
        item["transcript_snippet"] = transcript or ""
        item["transcript_highlights"] = extract_transcript_highlights(
            transcript or "", core_topic,
        )

    return {"items": items}


def parse_youtube_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse YouTube search response to normalized format.

    Returns:
        List of item dicts ready for normalization.
    """
    return response.get("items", [])
