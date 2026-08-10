"""Tests for yt-dlp invocation safety flags."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from lib import youtube_yt


class _DummyProc:
    def __init__(self):
        self.pid = 12345
        self.returncode = 0

    def communicate(self, timeout=None):
        return "", ""

    def wait(self, timeout=None):
        return 0


class TestYtDlpFlags(unittest.TestCase):
    def test_search_ignores_global_config_and_browser_cookies(self):
        proc = _DummyProc()
        with mock.patch.object(youtube_yt, "is_ytdlp_installed", return_value=True), \
             mock.patch.object(youtube_yt.subprocess, "Popen", return_value=proc) as popen_mock:
            youtube_yt.search_youtube("Claude Code", "2026-02-01", "2026-03-01")

        cmd = popen_mock.call_args.args[0]
        self.assertIn("--ignore-config", cmd)
        self.assertIn("--no-cookies-from-browser", cmd)

    def test_transcript_fetch_ignores_global_config_and_browser_cookies(self):
        proc = _DummyProc()
        with tempfile.TemporaryDirectory() as temp_dir, \
             mock.patch.object(youtube_yt.subprocess, "Popen", return_value=proc) as popen_mock:
            youtube_yt.fetch_transcript("abc123", temp_dir)

        cmd = popen_mock.call_args.args[0]
        self.assertIn("--ignore-config", cmd)
        self.assertIn("--no-cookies-from-browser", cmd)


class TestExtractTranscriptHighlights(unittest.TestCase):
    def test_extracts_specific_sentences(self):
        transcript = (
            "Hey guys welcome back to the channel. "
            "In today's video we're looking at something special. "
            "The Lego Bugatti Chiron took 13,438 hours to build with over 1 million pieces. "
            "Don't forget to subscribe and hit the bell. "
            "The tolerance on each brick is 0.002 millimeters which is insane for injection molding. "
            "So yeah that's pretty cool. "
            "Thanks for watching see you next time."
        )
        highlights = youtube_yt.extract_transcript_highlights(transcript, "Lego")
        self.assertTrue(len(highlights) > 0)
        # Should pick the sentences with numbers and topic relevance, not filler
        joined = " ".join(highlights)
        self.assertIn("13,438", joined)
        self.assertNotIn("subscribe", joined)
        self.assertNotIn("welcome back", joined)

    def test_empty_transcript(self):
        self.assertEqual(youtube_yt.extract_transcript_highlights("", "test"), [])

    def test_respects_limit(self):
        sentences = ". ".join(
            f"The model {i} has {i * 100} parameters and runs at {i * 10} tokens per second"
            for i in range(20)
        ) + "."
        highlights = youtube_yt.extract_transcript_highlights(sentences, "model", limit=3)
        self.assertEqual(len(highlights), 3)


class _ScriptedProc:
    """Popen stand-in that reports a scripted returncode and stderr."""

    def __init__(self, returncode=0, stderr="", on_run=None):
        self.pid = 12345
        self.returncode = returncode
        self._stderr = stderr
        self._on_run = on_run

    def communicate(self, timeout=None):
        if self._on_run:
            self._on_run()
        return "", self._stderr

    def wait(self, timeout=None):
        return 0


class TestTranscriptSubLangs(unittest.TestCase):
    """English-only subtitles silently dropped every non-English video.

    The fallback languages must be a SECOND yt-dlp run, not extra values on the
    first. Measured 2026-08-10: `--sub-lang en,es,pt` on an English video writes
    both en.vtt and es.vtt, because YouTube auto-translates. That is an extra
    request per video, and request volume is what triggers the 429s these
    changes are trying to survive.
    """

    def _sub_lang_calls(self, env=None):
        calls = []

        def popen_factory(cmd, **kwargs):
            calls.append(cmd[cmd.index("--sub-lang") + 1])
            return _ScriptedProc(returncode=0, stderr="")

        patch_env = mock.patch.dict(youtube_yt.os.environ, env or {}, clear=False)
        with tempfile.TemporaryDirectory() as temp_dir, patch_env, \
             mock.patch.object(youtube_yt.subprocess, "Popen", side_effect=popen_factory):
            if env is None:
                youtube_yt.os.environ.pop("LAST30DAYS_YT_SUB_LANGS", None)
            youtube_yt.fetch_transcript("abc123", temp_dir)
        return calls

    def test_first_attempt_requests_primary_language_alone(self):
        calls = self._sub_lang_calls()
        self.assertEqual(calls[0], "en")

    def test_fallback_languages_are_a_separate_later_attempt(self):
        calls = self._sub_lang_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], "es,pt")

    def test_env_var_overrides_languages(self):
        calls = self._sub_lang_calls({"LAST30DAYS_YT_SUB_LANGS": "en,de"})
        self.assertEqual(calls, ["en", "de"])

    def test_single_configured_language_makes_one_attempt(self):
        calls = self._sub_lang_calls({"LAST30DAYS_YT_SUB_LANGS": "en"})
        self.assertEqual(calls, ["en"])


class TestTranscriptRetry(unittest.TestCase):
    """A non-zero yt-dlp exit is a fetch error, not 'this video has no captions'.

    yt-dlp exits 0 with no file when a video genuinely lacks captions. A 429 or
    bot-gate exits non-zero, and retrying that is what recovers the transcript.
    """

    def _run(self, procs, write_vtt_on_attempt=None):
        attempts = {"n": 0}
        temp_dir_holder = {}

        def popen_factory(cmd, **kwargs):
            attempts["n"] += 1
            n = attempts["n"]

            def on_run():
                if write_vtt_on_attempt is not None and n >= write_vtt_on_attempt:
                    Path(temp_dir_holder["path"], "abc123.en.vtt").write_text(
                        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nrecovered text\n",
                        encoding="utf-8",
                    )

            spec = procs[min(n - 1, len(procs) - 1)]
            return _ScriptedProc(returncode=spec[0], stderr=spec[1], on_run=on_run)

        with tempfile.TemporaryDirectory() as temp_dir, \
             mock.patch.object(youtube_yt.subprocess, "Popen", side_effect=popen_factory), \
             mock.patch.object(youtube_yt.time, "sleep"):
            temp_dir_holder["path"] = temp_dir
            result = youtube_yt.fetch_transcript("abc123", temp_dir)
        return result, attempts["n"]

    def test_transient_429_is_retried_and_recovers(self):
        result, attempts = self._run(
            [(1, "ERROR: HTTP Error 429: Too Many Requests"), (0, "")],
            write_vtt_on_attempt=2,
        )
        self.assertIsNotNone(result)
        self.assertIn("recovered text", result)
        self.assertEqual(attempts, 2)

    def test_genuine_no_captions_is_not_retried(self):
        # Exit 0 and no file written == no captions in that language. The same
        # request is never repeated; the only follow-up is the fallback
        # language set, so this costs one attempt per language set and no more.
        result, attempts = self._run([(0, "")])
        self.assertIsNone(result)
        self.assertEqual(attempts, len(youtube_yt._sub_lang_attempts()))

    def test_rate_limit_does_not_escalate_to_more_language_requests(self):
        # A 429 is a request-volume problem. Trying another language set would
        # add load to the limiter that just rejected us.
        result, attempts = self._run([(1, "ERROR: HTTP Error 429: Too Many Requests")])
        self.assertIsNone(result)
        self.assertLessEqual(attempts, youtube_yt._TRANSCRIPT_MAX_RETRIES + 1)

    def test_partial_vtt_is_salvaged_despite_nonzero_exit(self):
        # yt-dlp fetched `en` fine, then 429'd on `es`, exiting non-zero with a
        # perfectly good English track already on disk.
        result, attempts = self._run(
            [(1, "ERROR: HTTP Error 429: Too Many Requests")],
            write_vtt_on_attempt=1,
        )
        self.assertIsNotNone(result)
        self.assertIn("recovered text", result)
        self.assertEqual(attempts, 1)


class TestTranscriptCandidateSelection(unittest.TestCase):
    """Transcript budget must not be starved by captionless or stale videos."""

    def test_candidates_overfetch_beyond_the_limit(self):
        items = [
            {"video_id": f"v{i}", "engagement": {"views": i * 100}, "date": "2026-08-01"}
            for i in range(30)
        ]
        picked = youtube_yt._select_transcript_candidates(items, 5)
        # More candidates than needed, because some videos lack captions.
        self.assertGreater(len(picked), 5)
        self.assertLessEqual(len(picked), 15)

    def test_recent_video_outranks_older_higher_view_video(self):
        items = [
            {"video_id": "old", "engagement": {"views": 1_000_000}, "date": "2020-01-01"},
            {"video_id": "new", "engagement": {"views": 5_000}, "date": "2026-08-09"},
        ]
        picked = youtube_yt._select_transcript_candidates(items, 1)
        self.assertIn("new", picked)

    def test_handles_missing_engagement_and_date(self):
        items = [{"video_id": "a"}, {"video_id": "b", "engagement": {}, "date": None}]
        picked = youtube_yt._select_transcript_candidates(items, 5)
        self.assertEqual(sorted(picked), ["a", "b"])


class TestTranscriptWaveFetching(unittest.TestCase):
    """Top up in waves rather than blindly overfetching.

    Fetching 3x the budget up front wastes yt-dlp calls on the happy path, and
    YouTube rate-limiting is the main reason transcripts fail at all. Only
    reach for more candidates when a wave comes back short.
    """

    def _run(self, captioned_ids, depth="quick"):
        items = [
            {"video_id": f"v{i}", "title": f"t{i}", "url": "",
             "engagement": {"views": 100 - i}, "date": "2026-08-01"}
            for i in range(20)
        ]
        requested = []

        def fake_parallel(video_ids, max_workers=5):
            requested.extend(video_ids)
            return {v: ("text" if v in captioned_ids else None) for v in video_ids}

        with mock.patch.object(youtube_yt, "search_youtube",
                               return_value={"items": items}), \
             mock.patch.object(youtube_yt, "fetch_transcripts_parallel",
                               side_effect=fake_parallel):
            result = youtube_yt.search_and_transcribe(
                "topic", "2026-07-10", "2026-08-09", depth=depth)
        got = sum(1 for i in result["items"] if i.get("transcript_snippet"))
        return got, requested

    def test_all_captioned_costs_exactly_the_budget(self):
        budget = youtube_yt.TRANSCRIPT_LIMITS["quick"]
        got, requested = self._run(captioned_ids={f"v{i}" for i in range(20)})
        self.assertEqual(got, budget)
        self.assertEqual(len(requested), budget)

    def test_captionless_videos_are_topped_up(self):
        budget = youtube_yt.TRANSCRIPT_LIMITS["quick"]
        # Only every 4th video has captions, so the first wave comes back short.
        captioned = {f"v{i}" for i in range(20) if i % 4 == 0}
        got, requested = self._run(captioned_ids=captioned)
        self.assertEqual(got, budget)
        self.assertGreater(len(requested), budget)

    def test_no_captions_anywhere_stops_at_candidate_cap(self):
        budget = youtube_yt.TRANSCRIPT_LIMITS["quick"]
        got, requested = self._run(captioned_ids=set())
        self.assertEqual(got, 0)
        # Bounded by the overfetch cap, not an infinite walk of all 20 videos.
        self.assertLessEqual(len(requested), budget * youtube_yt._TRANSCRIPT_OVERFETCH)


if __name__ == "__main__":
    unittest.main()
