# Security Audit: last30days-skill

- **Created:** 2026-03-18
- **Origin:** During a research session, macOS displayed a Keychain access prompt ("node wants to use your confidential information stored in Chrome Safe Storage"). Investigation revealed the vendored Bird search library was silently extracting browser cookies to authenticate with Twitter/X, and yt-dlp was attempting the same for YouTube. A full security audit of all scripts followed. This document exists so future upstream merges can be evaluated against known security boundaries.

## What Happened

The `last30days` skill runs Python scripts that spawn subprocesses for YouTube (yt-dlp) and X/Twitter (Node.js bird-search) data collection. Two of those subprocesses were silently accessing the macOS Keychain and browser cookie stores:

1. **Bird search** (X/Twitter) used a vendored npm package called `@steipete/sweet-cookie` that, when `AUTH_TOKEN`/`CT0` env vars were not set, fell back to extracting cookies directly from Safari, Chrome, and Firefox. For Chrome, this meant running `security find-generic-password` to extract the Chrome Safe Storage master key from the macOS Keychain, then using it to decrypt Chrome's entire encrypted cookie database. For Safari, it read the binary cookie file at `~/Library/Cookies/Cookies.binarycookies` which contains every cookie for every site.

2. **yt-dlp** (YouTube) was invoked without `--no-cookies-from-browser`, which meant it could read browser cookies depending on the user's yt-dlp configuration file.

Neither behavior was disclosed to the user. The macOS Keychain prompt (which only fires for Chrome, not Safari) was the only visible signal.

## Issues Found and Fixed

#### CRITICAL: Browser Cookie/Keychain Extraction via sweet-cookie

**Location:** `scripts/lib/vendor/bird-search/lib/cookies.js` and `scripts/lib/vendor/bird-search/node_modules/@steipete/sweet-cookie/`

**Problem:** The `resolveCredentials()` function had a three-tier fallback: CLI args, env vars, then browser extraction. Browser extraction probed Safari, Chrome, and Firefox in order. Chrome required Keychain access to decrypt cookies. Safari read ALL cookies (not just Twitter's) into memory before filtering. The entire browser session state for every site the user was logged into passed through this code path.

**Fix:**
- Deleted the `@steipete/sweet-cookie` package entirely from `node_modules/`
- Removed the `getCookies` import and all browser extraction functions (`readTwitterCookiesFromBrowser`, `extractCookiesFromSafari`, `extractCookiesFromChrome`, `extractCookiesFromFirefox`)
- Removed all dead helper code that only existed for browser extraction (`resolveSources`, `labelForSource`, `pickCookieValue`, `envFlagEnabled`, `TWITTER_COOKIE_NAMES`, `TWITTER_URL`, `TWITTER_ORIGINS`, `DEFAULT_COOKIE_TIMEOUT_MS`)
- `resolveCredentials()` now only reads CLI args and env vars. If credentials are missing, it warns and does not touch a browser.
- `cookies.js` went from 173 lines to 65.

#### HIGH: yt-dlp Keychain/Cookie Access

**Location:** `scripts/lib/youtube_yt.py` — two yt-dlp subprocess calls (search and transcript fetch)

**Problem:** yt-dlp was invoked without `--no-cookies-from-browser`. Depending on the user's `~/.config/yt-dlp/config`, yt-dlp could attempt to extract browser cookies (triggering Keychain access for Chrome).

**Fix:**
- Added `--no-cookies-from-browser` to both yt-dlp invocations
- Added `--ignore-config` to both invocations (prevents user's yt-dlp config file from overriding our flags — defense-in-depth, contributed by upstream)
- Added `--` separator before positional arguments to prevent topic strings starting with `-` from being interpreted as yt-dlp flags

#### MEDIUM: Full Parent Environment Leaked to Subprocesses

**Location:** `scripts/lib/bird_x.py` — `_subprocess_env()` function

**Problem:** `os.environ.copy()` passed every environment variable (database passwords, other API keys, cloud credentials, etc.) to Node.js subprocesses. The subprocess only needs `PATH`, `HOME`, and the Twitter credentials.

**Fix:** Replaced `os.environ.copy()` with an allowlist: `{"PATH", "HOME", "NODE_PATH", "TERM", "LANG"}` plus the injected `AUTH_TOKEN`/`CT0`.

#### MEDIUM: search_handles() Missing Credential Injection

**Location:** `scripts/lib/bird_x.py` — `search_handles()` function

**Problem:** The `search_handles()` subprocess.Popen call did not pass `env=_subprocess_env()`, meaning the Node subprocess inherited the default environment. Without `AUTH_TOKEN`/`CT0` in the environment, the (now-removed) cookies.js would fall back to browser extraction.

**Fix:** Added `env=_subprocess_env()` to the Popen call.

#### MEDIUM: Raw API Responses Written to Disk

**Location:** `scripts/lib/render.py` — `write_outputs()` function

**Problem:** Raw API responses from OpenAI, xAI, and Reddit were written to JSON files in `~/.local/share/last30days/out/`. Error responses from APIs can include request headers containing `Authorization: Bearer <key>`. These files persisted with no automatic cleanup.

**Fix:** Removed all raw response file writes. The function signature still accepts the parameters for backward compatibility but ignores them.

#### MEDIUM: Cache Files with Permissive Permissions

**Location:** `scripts/lib/cache.py`

**Problem:** Cache files were written with default permissions (typically 644, world-readable). The fallback path used `/tmp/last30days/cache/` which is world-readable on shared systems.

**Fix:**
- Cache directory created with `0o700` (owner-only access)
- All cache files set to `0o600` after writing
- `/tmp` fallback replaced with `tempfile.mkdtemp()` which creates owner-only directories

#### LOW: preexec_fn Deadlock Risk with Threads

**Location:** `scripts/lib/bird_x.py`, `scripts/lib/youtube_yt.py`

**Problem:** `preexec_fn=os.setsid` is called between `fork()` and the child process starting. In a multi-threaded program (the script uses `ThreadPoolExecutor`), this can deadlock if another thread holds a lock during fork.

**Fix:** Added `_PY311_PLUS = sys.version_info >= (3, 11)` check. Uses `process_group=0` (fork-safe) on Python 3.11+, falls back to `preexec_fn` on older versions.

#### LOW: Bare except Clauses

**Location:** `scripts/lib/http.py`, `scripts/lib/reddit_enrich.py`

**Problem:** `except:` catches `KeyboardInterrupt` and `SystemExit`, preventing clean shutdown.

**Fix:** Changed to `except Exception:`.

## What to Watch for in Future Upstream Merges

The fork monitor config (`fork-monitor-config.json` in `tahsinrk/claude-config`) includes these rules for AI analysis. But here they are in plain language for manual review:

#### Hard Rejections (merge will reintroduce vulnerabilities)

1. **Any re-addition of `@steipete/sweet-cookie`** or similar browser cookie extraction libraries in `node_modules/`. Check `package.json` and any new dependencies.

2. **Any import of browser cookie functions** in `cookies.js` or elsewhere. Watch for `getCookies`, `extractCookiesFromSafari`, `extractCookiesFromChrome`, `extractCookiesFromFirefox`, or any new function that reads from `~/Library/Cookies/`, Chrome's cookie database, or the macOS Keychain.

3. **yt-dlp calls missing `--no-cookies-from-browser`**. Every yt-dlp invocation must include this flag. Also check that `--ignore-config` is present (prevents user config from overriding).

4. **Reversion of `_subprocess_env()` to `os.environ.copy()`**. The allowlist pattern must be preserved. If new env vars are genuinely needed by subprocesses, add them to the allowlist explicitly — do not revert to copying everything.

5. **New subprocess.Popen calls missing `env=_subprocess_env()`**. Every Node subprocess must receive the minimal environment, not inherit the parent's.

6. **Raw API response writes to disk**. Any code that dumps API responses to files could leak auth headers.

#### Yellow Flags (review carefully before merging)

- New npm dependencies in the vendor directory (audit for what they access)
- New subprocess calls of any kind (check for `shell=True`, missing env, cookie access)
- Changes to `cache.py` or `render.py` file write paths (check permissions)
- New yt-dlp flags or config changes
- Any use of bare `except:`

## Current State (Post-Patch)

- `cookies.js`: 65 lines. Reads env vars and CLI args only. No browser access. No npm dependencies.
- `sweet-cookie`: Deleted entirely. Not in `node_modules`, not imported anywhere.
- `yt-dlp`: Called with `--no-cookies-from-browser`, `--ignore-config`, and `--` separator.
- Subprocess environment: Allowlisted to 5 system vars + 2 credential vars.
- Cache/output files: Owner-only permissions (0o600/0o700).
- No `shell=True`, no bare `except:`.
