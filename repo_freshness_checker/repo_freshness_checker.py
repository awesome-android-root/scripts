#!/usr/bin/env python3
"""
GitHub Repo Freshness Checker v2.4
===================================
Reads a file containing GitHub URLs, checks last-update dates via the
GitHub API, and writes a sorted Markdown report.  With a token it uses
GraphQL batch queries (up to 100 repos per request) for drastically
faster checking; without one it falls back to parallel REST requests
(ThreadPoolExecutor), so results stay identical either way.

  GUI mode : python repo_freshness_checker.py
  CLI mode : python repo_freshness_checker.py README.md -o report.md -t ghp_xxxx

"""

import os, re, sys, time, json, threading, argparse
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests as _requests
except ImportError:
    print("ERROR: 'requests' is required.\n  pip install requests")
    sys.exit(1)

# ──────────────────────────── config ────────────────────────────
VERSION           = "2.5.0"
CONFIG_DIR        = Path.home() / ".repo-freshness-checker"
TOKEN_FILE        = CONFIG_DIR / "token"
API_BASE          = "https://api.github.com"
DEFAULT_WORKERS   = 15       # concurrent API requests
REQUEST_TIMEOUT   = 15       # seconds per request
MAX_RETRIES       = 2        # max retries on rate-limit
GRAPHQL_BATCH_SIZE = 100     # repos per GraphQL request (~1 rate-limit point)
GRAPHQL_CONCURRENCY = 4      # max parallel GraphQL requests (each is already large)

# ──────────────────────── token helpers ─────────────────────────
def save_token(token: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Set umask so file is created with 0o600 from the start (prevents TOCTOU)
    old_umask = os.umask(0o177)
    try:
        TOKEN_FILE.write_text(token.strip(), encoding="utf-8")
    finally:
        os.umask(old_umask)
    # Also chmod for safety (handles existing files with wrong perms)
    if os.name != "nt":
        os.chmod(TOKEN_FILE, 0o600)

def load_token() -> str:
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    return os.environ.get("GITHUB_TOKEN", "")

def clear_saved_token():
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()


def validate_token(token: str) -> tuple[bool, str]:
    """Basic validation of GitHub token format. Returns (is_valid, message)."""
    if not token:
        return False, "Token is empty"
    t = token.strip()
    # Classic PATs: ghp_, gho_, ghu_, ghs_, ghr_
    # Fine-grained PATs: github_pat_
    if re.match(r'^(gh[opusr]_|github_pat_)', t):
        if len(t) < 20:
            return False, "Token seems too short (likely invalid)"
        return True, ""
    # Could be a token passed via env var in a different format
    if len(t) >= 40:
        return True, ""
    return False, "Doesn't look like a GitHub token (should start with ghp_ or github_pat_)"

# ──────────────────── URL extraction ────────────────────────────
_GH_URL_RE = re.compile(
    r"https?://github\.com/"
    r"([a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)"  # owner
    r"/"
    r"([a-zA-Z0-9._-]+)",                              # repo
    re.IGNORECASE,
)
_SKIP_SEGMENTS = frozenset(
    "issues pulls pull wiki wikis releases actions settings blob tree "
    "commit commits compare archive stargazers network graphs discussions "
    "packages projects security insights tags milestone milestones labels "
    "new edit delete raw suites check-runs deployments".split()
)

def extract_repos(filepaths: str | Path | Iterable[str | Path]) -> list[tuple[str, str]]:
    """Return de-duplicated [(owner, repo), …] from one or more files."""
    if isinstance(filepaths, (str, Path)):
        filepaths = [filepaths]

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for filepath in filepaths:
        text = Path(filepath).read_text(encoding="utf-8", errors="ignore")
        for m in _GH_URL_RE.finditer(text):
            owner, repo = m.group(1), m.group(2)
            repo = re.sub(r"\.git$", "", repo.rstrip("/"))
            if repo.lower() in _SKIP_SEGMENTS:
                continue
            key = f"{owner}/{repo}".lower()
            if key not in seen:
                seen.add(key)
                out.append((owner, repo))
    return out


def find_markdown_files(folder: str | Path) -> list[str]:
    """Return sorted Markdown files under *folder* with hidden directories ignored."""
    root = Path(folder)
    return sorted(
        str(path)
        for path in root.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def expand_input_paths(paths: list[str]) -> list[str]:
    """Expand directories to Markdown files; otherwise return the original paths."""
    expanded: list[str] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            expanded.extend(find_markdown_files(path))
        elif path.is_file():
            expanded.append(str(path))
    return expanded

# ──────────────────── GitHub API layer ──────────────────────────
def _session(token: str, *, graphql: bool = False) -> _requests.Session:
    s = _requests.Session()
    s.headers.update({
        # REST uses the versioned media type; the GraphQL endpoint just
        # returns JSON regardless.
        "Accept": "application/vnd.github.v3+json" if not graphql
                  else "application/json",
        "User-Agent": f"RepoFreshnessChecker/{VERSION}",
    })
    if token:
        s.headers["Authorization"] = f"token {token}"
    return s

def verify_auth(session: _requests.Session) -> dict | None:
    r = session.get(f"{API_BASE}/rate_limit", timeout=15)
    if r.status_code != 200:
        return None
    c = r.json()["resources"]["core"]
    return {
        "authed": "Authorization" in session.headers,
        "limit": c["limit"],
        "remaining": c["remaining"],
        "reset_ts": c["reset"],
    }

def _int_header(headers, name: str) -> int:
    """Parse an integer response header; 0 when missing / unparseable."""
    try:
        return int(headers.get(name) or 0)
    except ValueError:
        return 0

def _retry_after_seconds(headers) -> float | None:
    """GitHub sends 'Retry-After' (in seconds) when a secondary rate limit /
    abuse-detection limit is hit. Returns None when the header is absent."""
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0

def fetch_repo(session: _requests.Session, owner: str, repo: str) -> dict:
    """Fetch a single repo's info via the REST API (1 request per repo).

    Returns a dict with consistent keys. Rate-limited responses carry
    rate_limited=True plus either reset_ts (primary limit; epoch seconds)
    or retry_after (secondary limit; seconds to back off).
    """
    try:
        r = session.get(
            f"{API_BASE}/repos/{owner}/{repo}",
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            pa = d.get("pushed_at")
            return {
                "ok": True,
                "pushed_at": datetime.strptime(pa, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                ) if pa else None,
                "archived": d.get("archived", False),
                "stars": d.get("stargazers_count", 0),
                "description": d.get("description") or "",
            }
        if r.status_code == 404:
            return {"ok": False, "error": "Not found (deleted / private)"}
        if r.status_code == 451:
            return {"ok": False, "error": "Unavailable (DMCA / legal)"}
        if r.status_code in (403, 429):
            # Secondary rate limit / abuse detection: GitHub answers with a
            # Retry-After header telling us when we may try again - that must
            # be retried, not reported as a hard "Forbidden" error.
            retry_after = _retry_after_seconds(r.headers)
            if retry_after is not None:
                return {"ok": False, "error": "Secondary rate limit",
                        "rate_limited": True, "retry_after": retry_after}
            # Primary rate limit exhausted
            if r.status_code == 429 or \
                    r.headers.get("X-RateLimit-Remaining", "?") == "0":
                reset = _int_header(r.headers, "X-RateLimit-Reset")
                if not reset:
                    reset = int(time.time() + 60)
                label = "Rate-limited (429)" if r.status_code == 429 \
                    else "Rate-limited"
                return {"ok": False, "error": label,
                        "rate_limited": True, "reset_ts": reset}
            return {"ok": False, "error": "Forbidden (403)"}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except _requests.exceptions.Timeout:
        return {"ok": False, "error": "Timeout"}
    except _requests.exceptions.ConnectionError:
        return {"ok": False, "error": "Connection error"}
    except _requests.exceptions.RequestException as e:
        return {"ok": False, "error": f"Request failed: {e}"}

# ──────────────────── GraphQL batch layer ───────────────────────
def _graphql_usable(session: _requests.Session) -> bool:
    """True when the GraphQL API answers for this session/token.

    Probe is a single rateLimit query (~0 points; also served while the
    primary limit is exhausted). One retry covers transient failures.
    """
    query = "query { rateLimit { limit remaining resetAt } }"
    for _ in range(2):
        try:
            r = session.post(f"{API_BASE}/graphql", json={"query": query},
                             timeout=REQUEST_TIMEOUT)
        except _requests.exceptions.RequestException:
            r = None
        if r is not None and r.status_code == 200:
            try:
                payload = r.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                return True
        time.sleep(1.0)
    return False

def build_graphql_query(repos_batch: list[tuple[str, str]]) -> str:
    """One query with one aliased repository(owner, name) lookup per repo,
    so a whole batch of repos costs a single HTTP request (~1 point)."""
    parts = []
    for i, (owner, repo) in enumerate(repos_batch):
        parts.append(
            f'r{i}: repository(owner: {json.dumps(owner)}, '
            f'name: {json.dumps(repo)}) {{ '
            f'pushedAt isArchived stargazerCount description }}'
        )
    return "query {\n" + "\n".join(parts) + "\n}"

_ALIAS_FORBIDDEN_TYPES = frozenset(
    ("FORBIDDEN", "RESOURCE_NOT_ACCESSIBLE", "INSUFFICIENT_SCOPES")
)

def _alias_error_message(err, owner: str, repo: str) -> str:
    """Human-readable message for one alias-level GraphQL error."""
    if not isinstance(err, dict):
        return "GraphQL error"
    typ = err.get("type") or ""
    if typ == "NOT_FOUND":
        return "Not found (deleted / private)"
    if typ in _ALIAS_FORBIDDEN_TYPES:
        return "Forbidden (private / access denied)"
    msg = str(err.get("message") or "").strip()
    return (msg[:200] if msg else "GraphQL error")

def fetch_graphql_batch(
    session: _requests.Session,
    repos_batch: list[tuple[str, str]],
) -> tuple[list[dict] | None, dict | None]:
    """Fetch *repos_batch* (up to GRAPHQL_BATCH_SIZE repos) in ONE request.

    Returns (infos, batch_err):
      * success → (list of per-repo info dicts parallel to repos_batch, None).
        Alias-level errors (deleted/private repos, etc.) become per-repo
        error dicts - the rest of the batch is unaffected.
      * failure → (None, {error, ...}) with retry hints: rate_limited +
        reset_ts (primary limit), retry_after seconds (secondary limit), or
        immediate_rest (don't retry the batch - fall back to REST instead).
    """
    try:
        r = session.post(
            f"{API_BASE}/graphql",
            json={"query": build_graphql_query(repos_batch)},
            timeout=REQUEST_TIMEOUT,
        )
    except _requests.exceptions.Timeout:
        return None, {"error": "Timeout", "immediate_rest": True}
    except _requests.exceptions.ConnectionError:
        return None, {"error": "Connection error", "immediate_rest": True}
    except _requests.exceptions.RequestException as e:
        return None, {"error": f"Request failed: {e}", "immediate_rest": True}

    code = r.status_code
    if code == 401:
        return None, {"error": "Authentication failed (HTTP 401)",
                      "immediate_rest": True}
    if code in (502, 504):
        # GitHub kills requests exceeding its ~10s processing budget
        return None, {"error": f"Server timeout (HTTP {code})",
                      "immediate_rest": True}

    # Secondary rate limit / abuse detection (GitHub: HTTP 200 or 403 with a
    # Retry-After header, or an error message mentioning the secondary limit)
    retry_after = _retry_after_seconds(r.headers)
    if code in (403, 429) and retry_after is not None:
        return None, {"error": "Secondary rate limit", "retry_after": retry_after}
    if code == 403:
        if r.headers.get("X-RateLimit-Remaining") == "0":
            return None, {"error": "Rate-limited", "rate_limited": True,
                          "reset_ts": _int_header(r.headers, "X-RateLimit-Reset")
                                     or int(time.time() + 60)}
        return None, {"error": "Forbidden (403)"}
    if code == 429:
        return None, {"error": "Rate-limited (429)", "rate_limited": True,
                      "reset_ts": _int_header(r.headers, "X-RateLimit-Reset")
                                 or int(time.time() + 60)}
    if code != 200:
        return None, {"error": f"HTTP {code}"}

    try:
        payload = r.json()
    except ValueError:
        return None, {"error": "Invalid JSON response", "immediate_rest": True}
    if not isinstance(payload, dict):
        return None, {"error": "Invalid response payload", "immediate_rest": True}

    errors = payload.get("errors") or []
    data = payload.get("data")

    if isinstance(data, dict):
        infos: list[dict] = []
        for i, (owner, repo) in enumerate(repos_batch):
            node = data.get(f"r{i}")
            if isinstance(node, dict):
                pa = node.get("pushedAt")
                infos.append({
                    "ok": True,
                    "pushed_at": datetime.strptime(
                        pa, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc) if pa else None,
                    "archived": bool(node.get("isArchived", False)),
                    "stars": int(node.get("stargazerCount") or 0),
                    "description": node.get("description") or "",
                })
            else:
                alias_err = next(
                    (e for e in errors
                     if isinstance(e, dict) and e.get("path") == [f"r{i}"]),
                    None,
                )
                infos.append({"ok": False,
                              "error": _alias_error_message(
                                  alias_err, owner, repo)})
        return infos, None

    # Whole request failed (data == null)
    if any(isinstance(e, dict) and e.get("type") == "RATE_LIMITED"
           for e in errors):
        # GraphQL reports primary rate-limit exhaustion as HTTP 200 + an
        # error; the reset time comes from the response headers
        return None, {"error": "Rate-limited", "rate_limited": True,
                      "reset_ts": _int_header(r.headers, "X-RateLimit-Reset")
                                 or int(time.time() + 60)}
    if any(isinstance(e, dict)
           and "secondary rate limit" in str(e.get("message", "")).lower()
           for e in errors):
        return None, {"error": "Secondary rate limit", "retry_after": 60.0}
    msg = next((str(e["message"])[:200] for e in errors
                if isinstance(e, dict) and e.get("message")), "")
    return None, {"error": f"GraphQL error: {msg}" if msg
                  else "GraphQL request error",
                  "immediate_rest": True}

# ──────────────────── processing loop ───────────────────────────
def process_repos(
    repos: list[tuple[str, str]],
    token: str,
    progress_cb=None,   # (current, total) -> None
    log_cb=None,         # (msg: str) -> None
    cancel: threading.Event | None = None,
    max_workers: int = DEFAULT_WORKERS,
) -> tuple[list[dict], list[dict]]:
    """
    Check all repos and return (results, errors).

    With a token the tool fetches through the GraphQL API, where
    GRAPHQL_BATCH_SIZE repos are looked up per single request (~1 point) -
    a 500-repo list drops from 500 requests to ~5 and only needs a fraction
    of the rate-limit budget. When GraphQL is unavailable (no token, or the
    API rejects it) it falls back to one REST request per repo, parallelised
    with a thread pool. A failed GraphQL batch is retried across primary +
    secondary rate limits and finally rescued via REST, so no repo is lost.
    """
    session = _session(token)
    auth = verify_auth(session)
    if auth and log_cb:
        tag = "Authenticated" if auth["authed"] else "Unauthenticated"
        log_cb(f"🔑 {tag} - rate limit {auth['remaining']}/{auth['limit']}")

    results: list[dict] = []
    errors: list[dict] = []
    total = len(repos)
    if total == 0:
        return results, errors

    results_lock = threading.Lock()
    processed = 0
    processed_lock = threading.Lock()
    start_time = time.time()

    # Track remaining REST rate limit across threads (also used by the
    # REST fallback that rescues batches when GraphQL keeps failing)
    rl_remaining = auth["remaining"] if auth else None
    rl_reset_ts = auth["reset_ts"] if auth else 0
    rl_lock = threading.Lock()

    # Try GraphQL batching first (requires a token)
    gql_session: _requests.Session | None = None
    use_gql = False
    if token:
        g = _session(token, graphql=True)
        if _graphql_usable(g):
            gql_session, use_gql = g, True
        elif log_cb:
            log_cb("⚠ GraphQL API unavailable for this token - "
                   "falling back to REST (1 request per repo)")

    if use_gql:
        batch_size = GRAPHQL_BATCH_SIZE
        # Each GraphQL request already carries many repos, so cap how many
        # of those big requests run at once (secondary-rate-limit safety).
        pool_workers = max(1, min(max_workers, GRAPHQL_CONCURRENCY))
        if log_cb:
            n_req = (total + GRAPHQL_BATCH_SIZE - 1) // GRAPHQL_BATCH_SIZE
            log_cb(f"⚙  GraphQL batching: {total} repos → ~{n_req} request(s) "
                   f"({GRAPHQL_BATCH_SIZE} repos/request, "
                   f"≤{GRAPHQL_CONCURRENCY} concurrent)")
    else:
        batch_size = 1
        pool_workers = max_workers
    batches = [repos[i:i + batch_size] for i in range(0, total, batch_size)]

    def _rest_with_retries(owner: str, repo: str) -> dict:
        """Check one repo via REST, retrying primary/secondary rate limits."""
        nonlocal rl_remaining, rl_reset_ts

        if cancel and cancel.is_set():
            return {"ok": False, "error": "Cancelled"}

        # Pre-check the shared rate-limit counter (approximate) so threads
        # don't stampede the API when almost no requests are left
        with rl_lock:
            wait = 0.0
            if rl_remaining is not None and rl_remaining < 5:
                wait = max(0, rl_reset_ts - time.time()) + 5
        if wait > 0:
            if log_cb:
                log_cb(f"⏳ Rate-limit low - waiting {wait:.0f}s …")
            _interruptible_sleep(wait, cancel)
            if cancel and cancel.is_set():
                return {"ok": False, "error": "Cancelled"}
            new_auth = verify_auth(session)
            if new_auth:
                with rl_lock:
                    rl_remaining = new_auth["remaining"]
                    rl_reset_ts = new_auth["reset_ts"]

        for attempt in range(MAX_RETRIES):
            if cancel and cancel.is_set():
                return {"ok": False, "error": "Cancelled"}

            info = fetch_repo(session, owner, repo)

            with rl_lock:
                if rl_remaining is not None:
                    rl_remaining -= 1

            if not info.get("rate_limited"):
                return info

            # Rate-limited: honour the secondary Retry-After, or the
            # primary limit's reset time
            retry_after = info.get("retry_after")
            if retry_after is not None:
                wait = float(retry_after) + 1
                msg = f"⏳ Secondary rate limit - waiting {wait:.0f}s …"
            else:
                wait = max(0, info.get("reset_ts", time.time() + 60)) - time.time() + 5
                msg = f"⏳ Rate-limited - waiting {wait:.0f}s …"
            if log_cb:
                log_cb(msg)
            _interruptible_sleep(wait, cancel)
            if cancel and cancel.is_set():
                return {"ok": False, "error": "Cancelled"}

            # Re-check auth after the wait
            new_auth = verify_auth(session)
            if new_auth:
                with rl_lock:
                    rl_remaining = new_auth["remaining"]
                    rl_reset_ts = new_auth["reset_ts"]

        # All retries exhausted
        return {"ok": False, "error": "Rate-limited (retries exhausted)"}

    def _process_batch(pairs: list[tuple[str, str]]) -> list[dict] | None:
        """Check one batch; returns per-repo info dicts, or None if cancelled."""
        if cancel and cancel.is_set():
            return None

        if gql_session is not None:
            fallback_reason: str | None = None
            for attempt in range(MAX_RETRIES + 1):
                if cancel and cancel.is_set():
                    return None

                infos, blk = fetch_graphql_batch(gql_session, pairs)
                if blk is None:
                    return infos

                reason = blk.get("error", "GraphQL error")
                if blk.get("immediate_rest"):
                    # No point hammering a query GitHub just killed
                    fallback_reason = reason
                    break

                if blk.get("rate_limited"):
                    wait = max(0.0, blk.get("reset_ts", time.time() + 60)
                               - time.time()) + 5
                elif blk.get("retry_after") is not None:
                    wait = float(blk["retry_after"]) + 1
                else:
                    wait = 1.5 * (attempt + 1)   # transient error: back off
                if log_cb:
                    log_cb(f"⏳ {reason} - waiting {wait:.0f}s …")
                _interruptible_sleep(wait, cancel)
                if cancel and cancel.is_set():
                    return None
            else:
                fallback_reason = "retries exhausted"
            if log_cb:
                log_cb(f"⚠ GraphQL ({fallback_reason}) - retrying "
                       f"{len(pairs)} repo(s) via REST")

        # REST fallback (or REST-only mode): one request per repo
        infos = []
        for owner, repo in pairs:
            if cancel and cancel.is_set():
                return None
            infos.append(_rest_with_retries(owner, repo))
        return infos

    with ThreadPoolExecutor(max_workers=pool_workers) as executor:
        future_map = {
            executor.submit(_process_batch, batch): batch
            for batch in batches
        }

        for future in as_completed(future_map):
            if cancel and cancel.is_set():
                # Cancel remaining futures
                for f in future_map:
                    f.cancel()
                if log_cb:
                    log_cb("⛔ Cancelled.")
                break

            infos = future.result()
            if infos is None:
                continue

            for info, (owner, repo) in zip(infos, future_map[future]):
                with processed_lock:
                    processed += 1
                    cur = processed

                # ETA calculation
                elapsed = time.time() - start_time
                rate = cur / elapsed if elapsed > 0 else 0
                eta_secs = (total - cur) / rate if rate > 0 else 0

                dyn_info = {"cur": cur, "total": total,
                            "eta": eta_secs, "rate": rate}

                if progress_cb:
                    progress_cb(cur, total, dyn_info)

                if info["ok"]:
                    ds = (info["pushed_at"].strftime("%Y-%m-%d")
                          if info["pushed_at"] else "N/A")
                    arc = " [ARCHIVED]" if info["archived"] else ""
                    with results_lock:
                        results.append({
                            "name": f"{owner}/{repo}",
                            "date": info["pushed_at"],
                            "url": f"https://github.com/{owner}/{repo}",
                            "archived": info["archived"],
                            "stars": info["stars"],
                        })
                    if log_cb:
                        log_cb(f"[{cur}/{total}] ✅ {owner}/{repo}  -  {ds}{arc}")
                else:
                    with results_lock:
                        errors.append({
                            "name": f"{owner}/{repo}",
                            "url": f"https://github.com/{owner}/{repo}",
                            "error": info["error"],
                        })
                    if log_cb:
                        log_cb(f"[{cur}/{total}] ❌ {owner}/{repo}  -  {info['error']}")

    return results, errors


def _interruptible_sleep(seconds, cancel):
    end = time.time() + seconds
    while time.time() < end:
        if cancel and cancel.is_set():
            return
        time.sleep(0.5)

# ──────────────────── report generation ─────────────────────────
def _age_str(days: int) -> str:
    y, rem = divmod(days, 365)
    m = rem // 30
    if y:
        return f"{y}y {m}m"
    if m:
        return f"{m}m"
    return f"{days}d"

def _status(days: int, archived: bool) -> str:
    if archived:
        return "📦 Archived"
    if days > 5 * 365:
        return "🔴 5 + yrs"
    if days > 3 * 365:
        return "🟠 3-5 yrs"
    if days > 365:
        return "🟡 1-3 yrs"
    return "🟢 Active"

def _health_score(results: list[dict]) -> str:
    """Return an overall health emoji based on results distribution."""
    if not results:
        return "❓"
    active = sum(1 for r in results if r.get("date") and not r["archived"]
                 and (datetime.now(timezone.utc) - r["date"]).days <= 365)
    ratio = active / len(results)
    if ratio >= 0.9:
        return "🟢 Excellent"
    if ratio >= 0.7:
        return "🟡 Fair"
    if ratio >= 0.4:
        return "🟠 Poor"
    return "🔴 Critical"


def generate_report(results: list[dict], errors: list[dict], path: str):
    """Generate a polished Markdown report sorted oldest → newest."""
    now = datetime.now(timezone.utc)
    results.sort(
        key=lambda r: r["date"] or datetime.min.replace(tzinfo=timezone.utc)
    )

    counts: dict[str, int] = {
        "🟢 Active (< 1 yr)": 0,
        "🟡 Aging (1-3 yrs)": 0,
        "🟠 Old (3-5 yrs)": 0,
        "🔴 Stale (5 + yrs)": 0,
        "📦 Archived": 0,
    }
    for r in results:
        if r["archived"]:
            counts["📦 Archived"] += 1
        elif r["date"]:
            d = (now - r["date"]).days
            if d > 5*365:   counts["🔴 Stale (5 + yrs)"] += 1
            elif d > 3*365: counts["🟠 Old (3-5 yrs)"]   += 1
            elif d > 365:   counts["🟡 Aging (1-3 yrs)"]  += 1
            else:           counts["🟢 Active (< 1 yr)"]  += 1

    total_ok = len(results)
    total_err = len(errors)
    total = total_ok + total_err

    L: list[str] = []
    L.append(f"# 📊 Repository Freshness Report\n")
    L.append("<div align=\"center\">\n")
    L.append(f"> **Generated:** {now.strftime('%Y-%m-%d %H:%M UTC')}  ")
    L.append(f"> **Repos checked:** {total}  ")
    L.append(f"> **Health:** {_health_score(results)}  ")
    L.append("</div>\n")
    L.append("---\n")

    # Summary
    L.append("## 📋 Summary\n")
    L.append("| Category | Count | Share |")
    L.append("|----------|------:|-----:|")
    ok_total = sum(counts.values())
    for k, v in counts.items():
        share = f"{v/ok_total*100:.1f}%" if ok_total else "-"
        L.append(f"| {k} | {v} | {share} |")
    L.append(f"| **Total OK** | **{total_ok}** | |")
    if errors:
        L.append(f"| ❌ Errors | {total_err} | |")
    L.append("")

    # Overall stats row
    total_stars = sum(r.get("stars", 0) for r in results)
    avg_stars = total_stars / total_ok if total_ok else 0
    L.append("> ")
    L.append(f"> ⭐ **{total_stars:,}** total stars  ·  "
             f"📦 **{counts['📦 Archived']}** archived  ·  "
             f"⚠️ **{total_err}** errors")
    L.append("")

    L.append("---\n")

    # Detailed table
    L.append("## 📌 Repositories (oldest → newest)\n")
    L.append("| # | Repository | Last Commit | Age | ⭐ | Status |")
    L.append("|---|-----------|:-----------|:----|---:|--------|")
    for i, r in enumerate(results, 1):
        if r["date"]:
            d = (now - r["date"]).days
            ds = r["date"].strftime("%Y-%m-%d")
            age = _age_str(d)
            st  = _status(d, r["archived"])
        else:
            ds, age, st = "N/A", "-", "❓"
        L.append(f"| {i} | [{r['name']}]({r['url']}) | {ds} | {age} | "
                 f"{r['stars']} | {st} |")

    if errors:
        L.append("\n---\n")
        L.append(f"## ⚠️ Errors ({len(errors)})\n")
        L.append("| # | Repository | Error |")
        L.append("|---|-----------|-------|")
        for i, e in enumerate(errors, 1):
            L.append(f"| {i} | [{e['name']}]({e['url']}) | {e['error']} |")

    L.append("")
    L.append("---")
    L.append(f"*Report generated by [Repo Freshness Checker]({API_BASE}) v{VERSION}*")

    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")

# ──────────────────── HTML report ──────────────────────────────
def generate_html_report(results: list[dict], errors: list[dict], path: str):
    """Generate a standalone HTML report (one self-contained file, vanilla JS).

    Row data is embedded once as JSON and rendered client-side into a
    sortable table with live search and status filter chips. Numeric sort
    keys travel with each row (no display-string parsing) and each render
    is a single batched innerHTML write, so lists of several thousand
    repositories stay responsive. Keyboard: '/' focuses search, 'Esc'
    clears it. No external assets, no network requests.
    """
    from html import escape as _esc

    now = datetime.now(timezone.utc)
    results.sort(
        key=lambda r: r["date"] or datetime.min.replace(tzinfo=timezone.utc)
    )

    # Status buckets (oldest → newest), mirroring the Markdown report.
    CATS = [
        ("active",   "Active",   "< 1 yr",   365),
        ("aging",    "Aging",    "1–3 yrs",  3 * 365),
        ("old",      "Old",      "3–5 yrs",  5 * 365),
        ("stale",    "Stale",    "5+ yrs",   None),
        ("archived", "Archived", "archived", None),
    ]

    def _bucket(r: dict) -> str:
        if r["archived"]:
            return "archived"
        if r.get("date"):
            d = (now - r["date"]).days
            if d > 5 * 365:   return "stale"
            if d > 3 * 365:   return "old"
            if d > 365:       return "aging"
            return "active"
        return "unknown"

    # ── stats ───────────────────────────────────────────────
    total_ok = len(results)
    total_err = len(errors)
    total = total_ok + total_err
    total_stars = sum(r.get("stars", 0) for r in results)
    active_yr = sum(1 for r in results if r.get("date") and not r["archived"]
                    and (now - r["date"]).days <= 365)

    cat_counts = [sum(1 for r in results if _bucket(r) == cid)
                  for cid, _l, _s, _u in CATS]

    ratio = active_yr / total_ok if total_ok else 0
    if ratio >= 0.9:
        health_label, health_class = "Excellent", "excellent"
    elif ratio >= 0.7:
        health_label, health_class = "Fair", "fair"
    elif ratio >= 0.4:
        health_label, health_class = "Poor", "poor"
    else:
        health_label, health_class = "Critical", "critical"

    # ── row data ────────────────────────────────────────────
    rows_data = []
    for r in results:
        if r["date"]:
            d = (now - r["date"]).days
            date = r["date"].strftime("%Y-%m-%d")
            age = _age_str(d)
            age_key = d
        else:
            date, age, age_key = "—", "—", -1
        rows_data.append({
            "name": r["name"],
            "url": r["url"],
            "date": date,
            "age": age,
            "age_key": age_key,
            "stars": int(r.get("stars") or 0),
            "status": _bucket(r),
        })

    err_rows = "".join(
        "<tr>"
        f"<td>{i}</td>"
        f"<td><a href='{_esc(e['url'])}' target='_blank' rel='noopener'>{_esc(e['name'])}</a></td>"
        f"<td class='err-msg'>{_esc(e['error'])}</td>"
        "</tr>"
        for i, e in enumerate(errors, 1)
    )

    # ── chips (status filter + live counts) ─────────────────
    DOT = {"active": "green", "aging": "amber", "old": "orange",
           "stale": "red", "archived": "gray"}
    label_for = {cid: label for cid, label, _s, _u in CATS}
    chip_rows = (
        "<button type=\"button\" class=\"chip on\" data-id=\"all\">"
        "All <span class=\"n\">%d</span></button>" % total_ok
        + "".join(
            "<button type=\"button\" class=\"chip\" data-id=\"%s\">"
            "<span class=\"cdot %s\"></span>%s "
            "<span class=\"n\">%d</span></button>"
            % (cid, DOT[cid], label_for[cid], n)
            for cid, n in zip([c for c, _l, _s, _u in CATS], cat_counts)
        )
    )

    errors_html = ""
    if errors:
        errors_html = f"""<details class="panel err-panel" open>
<summary><span class="sum-in"><span class="chev">▶</span> Errors<span class="errs">{total_err}</span></span></summary>
<div class="tblwrap"><table class="tbl">
<thead><tr><th style="width:48px"><div class="ti">#</div></th>
<th><div class="ti">Repository</div></th>
<th><div class="ti">Error</div></th></tr></thead>
<tbody>{err_rows}</tbody></table></div>
</details>"""

    # ── inline CSS ──────────────────────────────────────────
    CSS = """\
:root{
  --bg:#f5f6f8;--panel:#fff;--panel-2:#fafbfc;--ink:#1b1f24;--ink-2:#57606a;--ink-3:#8b949e;
  --line:#d8dee4;--line-2:#eaeef2;--accent:#2563eb;--accent-weak:#e9effd;--hl:#f3f4f6;
  --green:#1a7f37;--green-weak:#dcfce7;--amber:#9a6700;--amber-weak:#fef3c7;
  --orange:#bc4c00;--orange-weak:#ffedd5;--red:#cf222e;--red-weak:#ffe5e5;
  --gray:#656d76;--gray-weak:#eef0f2;
  --shadow:0 1px 2px rgba(16,24,40,.05);--radius:12px;
}
@media (prefers-color-scheme:dark){
:root{
  --bg:#0d1117;--panel:#161b22;--panel-2:#10151c;--ink:#e6edf3;--ink-2:#9da7b3;--ink-3:#6e7681;
  --line:#30363d;--line-2:#21262d;--accent:#3d84f7;--accent-weak:#15233f;--hl:#1c2128;
  --green:#3fb950;--green-weak:#0f2b18;--amber:#d29922;--amber-weak:#2b2310;
  --orange:#db6d28;--orange-weak:#2d1a0d;--red:#f85149;--red-weak:#331214;
  --gray:#8b949e;--gray-weak:#21262d;--shadow:0 1px 3px rgba(0,0,0,.35);
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,"Helvetica Neue",Arial,sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
button{font:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.wrap{max-width:1160px;margin:0 auto;padding:28px 20px 56px}

/* header */
.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0 0 18px}
h1{font-size:20px;font-weight:650;letter-spacing:-.01em;margin:0}
.health{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;border-radius:999px;
font-size:12px;font-weight:600}
.health::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.health.excellent{background:var(--green-weak);color:var(--green)}
.health.fair{background:var(--amber-weak);color:var(--amber)}
.health.poor{background:var(--orange-weak);color:var(--orange)}
.health.critical{background:var(--red-weak);color:var(--red)}
.meta{margin-left:auto;color:var(--ink-3);font-size:12px;font-variant-numeric:tabular-nums}

/* stat cards */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
padding:13px 15px;box-shadow:var(--shadow)}
.card .v{font-size:21px;font-weight:650;line-height:1.15;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.card .v em{font-style:normal;font-size:12px;color:var(--ink-3);font-weight:550}
.card .k{font-size:11px;color:var(--ink-2);margin-top:3px;text-transform:uppercase;letter-spacing:.05em}

/* panel & toolbar */
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
box-shadow:var(--shadow);overflow:hidden;margin-bottom:14px}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px;border-bottom:1px solid var(--line-2)}
.search{position:relative;flex:1 1 200px;min-width:180px}
.search svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--ink-3);pointer-events:none}
#searchBox{width:100%;padding:7px 10px 7px 30px;border:1px solid var(--line);border-radius:8px;
background:var(--panel-2);color:var(--ink);font:inherit;font-size:13px;outline:none}
#searchBox:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-weak)}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--ink);padding:6px 11px;
border-radius:8px;font-size:13px;font-weight:500;cursor:pointer;display:inline-flex;gap:6px;
align-items:center;white-space:nowrap}
.btn:hover{background:var(--hl)}
.btn:active{transform:translateY(1px)}
.count{margin-left:auto;color:var(--ink-3);font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap}

/* filter chips */
.chips{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding:10px 12px;border-bottom:1px solid var(--line-2)}
.chip{border:1px solid var(--line);background:var(--panel);color:var(--ink-2);padding:4px 11px;
border-radius:999px;font-size:12.5px;cursor:pointer;display:inline-flex;gap:6px;align-items:center}
.chip:hover{border-color:var(--ink-3);color:var(--ink)}
.chip .n{font-variant-numeric:tabular-nums;color:var(--ink-3);font-size:11.5px}
.chip.on{border-color:var(--accent);background:var(--accent-weak);color:var(--ink);font-weight:550}
.chip.on .n{color:var(--accent)}
.cdot{width:7px;height:7px;border-radius:50%;background:var(--gray)}
.cdot.green{background:var(--green)}.cdot.amber{background:var(--amber)}
.cdot.orange{background:var(--orange)}.cdot.red{background:var(--red)}

/* table */
.tblwrap{overflow-x:auto}
.tbl{width:100%;border-collapse:collapse;font-size:13.5px}
.tbl th{position:sticky;top:0;background:var(--panel);text-align:left;font-weight:600;
font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-2);padding:0;
border-bottom:1px solid var(--line);white-space:nowrap;cursor:pointer;user-select:none;z-index:1}
.tbl th .ti{display:flex;align-items:center;gap:5px;padding:9px 12px}
.tbl th:hover{color:var(--ink)}
.tbl th .arr{font-size:8px;color:var(--accent);opacity:0}
.tbl th.sorted{color:var(--ink)}
.tbl th.sorted .arr{opacity:1}
.tbl td{padding:8px 12px;border-bottom:1px solid var(--line-2);vertical-align:middle}
.tbl tbody tr:nth-child(even){background:var(--panel-2)}
.tbl tbody tr:hover{background:var(--hl)}
.tbl tbody tr:last-child td{border-bottom:0}
.tbl .date{white-space:nowrap;color:var(--ink-2);font-variant-numeric:tabular-nums}
.tbl .age{white-space:nowrap;color:var(--ink-2)}
.tbl .stars{white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums}
.repo-name{font-weight:550;overflow-wrap:anywhere}
.stars .ic{vertical-align:-2px;margin-right:5px}
.status{display:inline-flex;align-items:center;gap:7px;white-space:nowrap;font-size:12.5px}
.dot{width:7px;height:7px;border-radius:50%;flex:none;background:var(--gray)}
.dot.green{background:var(--green)}.dot.amber{background:var(--amber)}
.dot.orange{background:var(--orange)}.dot.red{background:var(--red)}
.err-msg{color:var(--red);overflow-wrap:anywhere}
.empty{padding:36px 12px;text-align:center;color:var(--ink-3)}

/* collapsible errors */
.err-panel>summary{list-style:none;cursor:pointer}
.err-panel>summary::-webkit-details-marker{display:none}
.sum-in{display:flex;align-items:center;gap:9px;padding:11px 14px;font-weight:600;user-select:none}
.sum-in:hover{background:var(--hl)}
.chev{color:var(--ink-3);font-size:9px;transition:transform .15s}
.err-panel[open] .chev{transform:rotate(90deg)}
.errs{margin-left:auto;background:var(--red-weak);color:var(--red);border-radius:999px;
font-size:11.5px;font-weight:600;padding:1px 9px;font-variant-numeric:tabular-nums}

.footer{margin-top:26px;text-align:center;color:var(--ink-3);font-size:12px}
.kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:0 5px;
font-size:11px;color:var(--ink-2);font-family:inherit}
.toast{position:fixed;left:50%;bottom:24px;transform:translate(-50%,10px);background:var(--ink);
color:var(--bg);padding:8px 16px;border-radius:9px;font-size:13px;opacity:0;pointer-events:none;
transition:.2s ease;z-index:20}
.toast.show{opacity:1;transform:translate(-50%,0)}

@media (max-width:840px){
  .cards{grid-template-columns:repeat(2,1fr)}
  .meta{margin-left:0;width:100%}
  .count{display:none}
}
"""

    # ── inline JS ───────────────────────────────────────────
    # __DATA__ is substituted below. '<' is escaped in the JSON so a repo
    # name can never close the script tag.
    JS = r"""
const data = __DATA__;
const STATUSES = ["active", "aging", "old", "stale", "archived"];
const STATUS_LABEL = {active: "Active", aging: "Aging", old: "Old",
                      stale: "Stale", archived: "Archived"};
const DOT_CLASS = {active: "green", aging: "amber", old: "orange",
                   stale: "red", archived: "gray"};
const STAR_SVG = '<svg class="ic" width="11" height="11" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97.719 4.192a.75.75 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 .25Z"/></svg>';

let sortCol = "age";
let sortAsc = false;  // oldest first by default
let stFilter = "all";
let q = "";

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
function shortStars(n) {
  return n >= 1000 ? (n / 1000).toFixed(n % 1000 === 0 ? 0 : 1) + "k" : String(n);
}
function matches(r) {
  if (stFilter !== "all" && r.status !== stFilter) return false;
  if (q && !(r.name.toLowerCase().includes(q) || r.date.includes(q))) return false;
  return true;
}

function render() {
  const tbody = document.getElementById("repoBody");
  const dir = sortAsc ? 1 : -1;
  const sorted = data.slice().sort((a, b) => {
    let va, vb;
    if (sortCol === "name")        { va = a.name.toLowerCase(); vb = b.name.toLowerCase(); }
    else if (sortCol === "stars")  { va = a.stars; vb = b.stars; }
    else if (sortCol === "status") { va = STATUSES.indexOf(a.status); vb = STATUSES.indexOf(b.status); }
    else if (sortCol === "date")   { va = a.date === "—" ? "" : a.date; vb = b.date === "—" ? "" : b.date; }
    else                           { va = a.age_key; vb = b.age_key; }
    if (va < vb) return -dir;
    if (va > vb) return dir;
    return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
  });

  let html = "";
  let shown = 0;
  for (const r of sorted) {
    if (!matches(r)) continue;
    shown++;
    html += "<tr>"
      + "<td><a class=\"repo-name\" href=\"" + esc(r.url) + "\" target=\"_blank\" rel=\"noopener\">" + esc(r.name) + "</a></td>"
      + "<td class=\"date\">" + r.date + "</td>"
      + "<td class=\"age\">" + r.age + "</td>"
      + "<td class=\"stars\" title=\"" + r.stars.toLocaleString() + " stars\">" + STAR_SVG + shortStars(r.stars) + "</td>"
      + "<td><span class=\"status\"><span class=\"dot " + (DOT_CLASS[r.status] || "") + "\"></span>" + (STATUS_LABEL[r.status] || r.status) + "</span></td>"
      + "</tr>";
  }
  tbody.innerHTML = shown ? html
    : "<tr><td colspan=\"5\" class=\"empty\">No repositories match the current search or filter.</td></tr>";
  document.getElementById("count").textContent = shown + " / " + data.length;
}

function setFilter(id) {
  stFilter = id;
  document.querySelectorAll("#chips .chip").forEach(c =>
    c.classList.toggle("on", c.dataset.id === id));
  render();
}
function sortBy(col) {
  if (sortCol === col) sortAsc = !sortAsc;
  else { sortCol = col; sortAsc = true; }
  document.querySelectorAll(".tbl th[data-col]").forEach(th => {
    const on = th.dataset.col === sortCol;
    th.classList.toggle("sorted", on);
    th.querySelector(".arr").textContent = on ? (sortAsc ? "▲" : "▼") : "";
  });
  render();
}

function exportCSV() {
  const rows = [["Repository", "URL", "Last commit", "Age (days)", "Stars", "Status"]];
  for (const r of data) {
    if (!matches(r)) continue;
    rows.push([r.name, r.url, r.date,
               r.age_key >= 0 ? String(r.age_key) : "",
               String(r.stars), STATUS_LABEL[r.status] || r.status]);
  }
  const csv = rows.map(row => row.map(c => {
    c = String(c == null ? "" : c);
    return /[",\n]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c;
  }).join(",")).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["\ufeff" + csv],
                                        {type: "text/csv;charset=utf-8"}));
  a.download = "repo-freshness-report.csv";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 300);
  const t = document.getElementById("toast");
  t.textContent = "Exported " + (rows.length - 1) + " rows to CSV";
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 1800);
}

function init() {
  document.getElementById("searchBox").addEventListener("input", e => {
    q = e.target.value.trim().toLowerCase();
    render();
  });
  document.getElementById("clearBtn").addEventListener("click", () => {
    document.getElementById("searchBox").value = "";
    q = "";
    setFilter("all");
  });
  document.getElementById("exportBtn").addEventListener("click", exportCSV);
  document.querySelectorAll("#chips .chip").forEach(chip => {
    chip.addEventListener("click", () => setFilter(chip.dataset.id));
  });
  document.querySelectorAll(".tbl th[data-col]").forEach(th => {
    th.tabIndex = 0;
    th.addEventListener("click", () => sortBy(th.dataset.col));
    th.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        sortBy(th.dataset.col);
      }
    });
  });
  document.addEventListener("keydown", e => {
    const box = document.getElementById("searchBox");
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing) { e.preventDefault(); box.focus(); }
    if (e.key === "Escape" && document.activeElement === box) {
      box.value = ""; q = ""; render(); box.blur();
    }
  });
  render();
}
document.addEventListener("DOMContentLoaded", init);
"""

    rows_json = json.dumps(rows_data).replace("<", "\\u003c")
    js = JS.replace("__DATA__", rows_json)

    HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<title>Repository Freshness Report</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <h1>Repository Freshness</h1>
  <span class="health {health_class}">{health_label}</span>
  <span class="meta">{total} repos · generated {now.strftime('%Y-%m-%d %H:%M UTC')}</span>
</header>

<section class="cards" aria-label="Summary">
  <div class="card"><div class="v">{total:,}</div><div class="k">Repos checked</div></div>
  <div class="card"><div class="v">{active_yr:,} <em>/ {total_ok}</em></div><div class="k">Active in last year</div></div>
  <div class="card"><div class="v">{total_stars:,}</div><div class="k">Total stars</div></div>
  <div class="card"><div class="v">{total_err}</div><div class="k">Errors</div></div>
</section>

<section class="panel">
  <div class="toolbar">
    <div class="search">
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="7" cy="7" r="4.75"/><path d="m10.9 10.9 3.3 3.3"/></svg>
      <input type="search" id="searchBox" placeholder="Search name or date…" autocomplete="off" aria-label="Search repositories">
    </div>
    <button type="button" class="btn" id="clearBtn">Reset</button>
    <button type="button" class="btn" id="exportBtn" title="Download the currently visible rows as CSV">
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M8 2.5v7m0 0L5 6.8m3 2.7 3-2.7M2.5 11.5v1.2A1.3 1.3 0 0 0 3.8 14h8.4a1.3 1.3 0 0 0 1.3-1.3v-1.2"/></svg>
      CSV
    </button>
    <span class="count" id="count">—</span>
  </div>
  <div class="chips" id="chips" role="group" aria-label="Filter by status">
    {chip_rows}
  </div>
  <div class="tblwrap">
    <table class="tbl">
      <thead>
        <tr>
          <th data-col="name"><div class="ti">Repository<span class="arr"></span></div></th>
          <th data-col="date"><div class="ti">Last commit<span class="arr"></span></div></th>
          <th data-col="age" class="sorted"><div class="ti">Age<span class="arr">▼</span></div></th>
          <th data-col="stars"><div class="ti" style="justify-content:flex-end">Stars<span class="arr"></span></div></th>
          <th data-col="status"><div class="ti">Status<span class="arr"></span></div></th>
        </tr>
      </thead>
      <tbody id="repoBody"></tbody>
    </table>
  </div>
</section>

{errors_html}

<div class="footer">
  Repo Freshness Checker v{VERSION} · generated {now.strftime('%Y-%m-%d %H:%M UTC')} ·
  <span class="kbd">/</span> focus search · <span class="kbd">esc</span> clear
</div>
<div class="toast" id="toast" role="status"></div>
<script>{js}</script>
</body>
</html>"""

    Path(path).write_text(HTML, encoding="utf-8")


# ═══════════════════════════ GUI ════════════════════════════════
def run_gui():
    """Dark, flat tkinter GUI.  Native widgets only — ttk theming plus
    plain tk controls styled through the option database and a tiny
    palette, so it looks the same on Windows / macOS / Linux."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    import webbrowser

    # ── palette ──────────────────────────────────────────────
    class Palette:
        BG       = "#101014"   # window
        SURFACE  = "#191920"   # panels / bars
        SURF_ALT = "#22222b"   # fields, chips
        SURF_DEEP= "#0c0c10"   # log canvas
        BORDER   = "#30303b"   # hairlines, field outlines
        TEXT     = "#e7eaf0"   # primary text
        SUBTEXT  = "#97a0b0"   # secondary text
        FAINT    = "#626b7a"   # hints, placeholders
        ACCENT   = "#5b9dff"   # interactive blue
        ACCENT_H = "#7ab4ff"
        ACCENT_P = "#4a86e0"
        ONACCENT = "#0b1017"
        GREEN    = "#7ee2a8"
        RED      = "#ff7b72"
        YELLOW   = "#e3b341"

    # ── ttk theme ────────────────────────────────────────────
    def _build_theme(style: ttk.Style) -> None:
        avail = style.theme_names()
        base = ("alt" if "alt" in avail else
                "clam" if "clam" in avail else "default")
        try:
            style.theme_use(base)
        except tk.TclError:
            pass
        name = "rfc"
        try:
            style.theme_create(name, base, {
                "TFrame":      {"configure": {"background": Palette.BG}},
                "TLabel":      {"configure": {"background": Palette.BG,
                                              "foreground": Palette.TEXT}},
                "TButton":     {"configure": {"background": Palette.SURFACE,
                                              "foreground": Palette.TEXT,
                                              "borderwidth": 0,
                                              "padding": (13, 6)},
                                "map": {"background": [("pressed", Palette.BORDER),
                                                       ("active", Palette.SURF_ALT),
                                                       ("disabled", Palette.SURFACE)],
                                        "foreground": [("disabled", Palette.FAINT)]}},
                "TCheckbutton": {"configure": {"background": Palette.BG,
                                               "foreground": Palette.SUBTEXT},
                                 "map": {"background": [("active", Palette.BG)]}},
                "TSpinbox":    {"configure": {"fieldbackground": Palette.SURF_ALT,
                                              "foreground": Palette.TEXT,
                                              "buttonbackground": Palette.SURF_ALT,
                                              "padding": (4, 3)}},
                "TProgressbar": {"configure": {"background": Palette.ACCENT,
                                               "troughcolor": Palette.SURF_ALT,
                                               "borderwidth": 0}},
                "Horizontal.TProgressbar":
                               {"configure": {"background": Palette.ACCENT,
                                              "troughcolor": Palette.SURF_ALT,
                                              "borderwidth": 0}},
            })
            style.theme_use(name)
        except tk.TclError:
            # fall back to configuring whatever theme is active
            pass

        style.configure("Accent.TButton",
                        background=Palette.ACCENT, foreground=Palette.ONACCENT,
                        font=("sans-serif", 10, "bold"), padding=(18, 7))
        style.map("Accent.TButton",
                  background=[("pressed", Palette.ACCENT_P),
                              ("active", Palette.ACCENT_H),
                              ("disabled", Palette.SURF_ALT)],
                  foreground=[("disabled", Palette.FAINT)])
        style.configure("Ghost.TButton",
                        background=Palette.BG, foreground=Palette.SUBTEXT,
                        padding=(10, 5))
        style.map("Ghost.TButton",
                  background=[("active", Palette.SURF_ALT),
                              ("pressed", Palette.BORDER),
                              ("disabled", Palette.BG)],
                  foreground=[("disabled", Palette.FAINT),
                              ("active", Palette.TEXT)])
        style.configure("Danger.TButton",
                        background=Palette.BG, foreground=Palette.RED,
                        borderwidth=0, padding=(13, 6))
        style.map("Danger.TButton",
                  background=[("active", Palette.SURFACE),
                              ("pressed", Palette.BORDER),
                              ("disabled", Palette.BG)],
                  foreground=[("disabled", Palette.FAINT)])
        style.configure("Title.TLabel",
                        font=("sans-serif", 15, "bold"),
                        foreground=Palette.TEXT)
        style.configure("Section.TLabel",
                        font=("sans-serif", 9, "bold"),
                        foreground=Palette.SUBTEXT)
        style.configure("Caption.TLabel",
                        font=("sans-serif", 8),
                        foreground=Palette.FAINT)
        style.configure("Status.TLabel",
                        background=Palette.SURFACE,
                        foreground=Palette.SUBTEXT)
        style.configure("Stats.TLabel",
                        background=Palette.SURFACE,
                        foreground=Palette.SUBTEXT,
                        font=("sans-serif", 9))

    class App(tk.Tk):
        CONFIG_FILE = CONFIG_DIR / "gui_config.json"

        # ── setup ────────────────────────────────────────────
        def __init__(self):
            super().__init__()
            self.title("Repo Freshness Checker")
            self.geometry("880x700")
            self.minsize(760, 560)

            self.cancel_event = threading.Event()
            self.running = False
            self.last_results: list | None = None
            self.last_errors: list | None = None
            self.input_files: list[str] = []

            self.configure(bg=Palette.BG)
            self._tk_base_fonts()
            style = ttk.Style(self)
            _build_theme(style)

            self._build()
            self._setup_drag_drop()

            self._load_config()
            saved = load_token()
            if saved:
                self.tok_var.set(saved)
                self.remember_var.set(True)

        def _tk_base_fonts(self):
            # Tk's native widgets keep a consistent font stack too
            try:
                import tkinter.font as tkfont
                base = tkfont.nametofont("TkDefaultFont")
                base.configure(family="sans-serif", size=10)
                ui = tkfont.nametofont("TkTextFont")
                ui.configure(family="sans-serif", size=10)
                f = tkfont.nametofont("TkFixedFont")
                f.configure(family="Consolas" if sys.platform == "win32"
                            else "Menlo" if sys.platform == "darwin"
                            else "monospace", size=10)
            except Exception:
                pass
            self.option_add("*Entry.background", Palette.SURF_ALT)
            self.option_add("*Entry.foreground", Palette.TEXT)
            self.option_add("*Entry.insertBackground", Palette.TEXT)
            self.option_add("*Entry.highlightBackground", Palette.BORDER)
            self.option_add("*Entry.highlightColor", Palette.ACCENT)
            self.option_add("*Entry.relief", "flat")
            self.option_add("*Entry.borderWidth", 0)
            self.option_add("*Text.background", Palette.SURF_DEEP)
            self.option_add("*Text.foreground", Palette.TEXT)
            self.option_add("*Text.insertBackground", Palette.TEXT)
            self.option_add("*Text.highlightBackground", Palette.BORDER)
            self.option_add("*Text.highlightColor", Palette.BORDER)
            self.option_add("*Text.relief", "flat")
            self.option_add("*Text.borderWidth", 0)
            self.option_add("*Listbox.background", Palette.SURF_ALT)
            self.option_add("*Listbox.foreground", Palette.TEXT)

        # ── small builders ───────────────────────────────────
        def _field(self, parent) -> tk.Entry:
            return tk.Entry(parent, bg=Palette.SURF_ALT, fg=Palette.TEXT,
                            insertbackground=Palette.TEXT, relief="flat",
                            borderwidth=0, highlightthickness=1,
                            highlightbackground=Palette.BORDER,
                            highlightcolor=Palette.ACCENT)

        # ── layout ───────────────────────────────────────────
        def _build(self):
            main = ttk.Frame(self, padding=(18, 14, 18, 10))
            main.pack(fill="both", expand=True)

            # header
            hdr = ttk.Frame(main)
            hdr.pack(fill="x")
            ttk.Label(hdr, text="Repo Freshness Checker",
                      style="Title.TLabel").pack(side="left")
            chip = tk.Label(hdr, text=f"v{VERSION}", bg=Palette.SURF_ALT,
                            fg=Palette.SUBTEXT, padx=8, pady=2,
                            font=("sans-serif", 8))
            chip.pack(side="left", padx=(9, 0))
            ttk.Label(hdr, text="GitHub repo activity → Markdown + HTML report",
                      style="Caption.TLabel").pack(side="right")

            self._rule(main)

            # ── source & target ──────────────────────────────
            grid = ttk.Frame(main)
            grid.pack(fill="x")
            grid.columnconfigure(0, weight=3, uniform="cols")
            grid.columnconfigure(1, weight=2, uniform="cols")

            # input
            left = ttk.Frame(grid)
            left.grid(row=0, column=0, sticky="ew", padx=(0, 12))
            ttk.Label(left, text="SOURCE", style="Section.TLabel").pack(anchor="w")
            row1 = ttk.Frame(left)
            row1.pack(fill="x", pady=(5, 0))
            self.in_var = tk.StringVar()
            self.in_entry = self._field(row1)
            self.in_entry.pack(side="left", fill="x", expand=True, ipady=4)
            ttk.Button(row1, text="Browse…", style="Ghost.TButton",
                       command=self._browse_in).pack(side="left", padx=(6, 0))
            self.src_hint = ttk.Label(
                left, style="Caption.TLabel",
                text="Markdown files or a folder containing them")
            self.src_hint.pack(anchor="w", pady=(3, 0))

            # output
            right = ttk.Frame(grid)
            right.grid(row=0, column=1, sticky="ew")
            ttk.Label(right, text="REPORT", style="Section.TLabel").pack(anchor="w")
            row2 = ttk.Frame(right)
            row2.pack(fill="x", pady=(5, 0))
            self.out_var = tk.StringVar(value="freshness_report.md")
            self.out_entry = self._field(row2)
            self.out_entry.pack(side="left", fill="x", expand=True, ipady=4)
            ttk.Button(row2, text="Browse…", style="Ghost.TButton",
                       command=self._browse_out).pack(side="left", padx=(6, 0))
            ttk.Label(right, style="Caption.TLabel",
                      text="Saved as .md — an HTML version is written beside it"
            ).pack(anchor="w", pady=(3, 0))

            # ── authentication ───────────────────────────────
            self._rule(main)
            auth = ttk.Frame(main)
            auth.pack(fill="x")
            ttk.Label(auth, text="TOKEN", style="Section.TLabel").pack(side="left")
            link = tk.Label(auth, text="Create a token →",
                            bg=Palette.BG, fg=Palette.ACCENT, cursor="hand2",
                            font=("sans-serif", 8))
            link.pack(side="right")
            link.bind("<Button-1>", lambda e: webbrowser.open(
                "https://github.com/settings/tokens/new?scopes=&description=repo-freshness-checker"))
            link.bind("<Enter>", lambda e: link.configure(font=("sans-serif", 8, "underline")))
            link.bind("<Leave>", lambda e: link.configure(font=("sans-serif", 8)))

            arow = ttk.Frame(main)
            arow.pack(fill="x", pady=(6, 0))
            self.tok_var = tk.StringVar()
            self.tok_entry = self._field(arow)
            self.tok_entry.pack(side="left", fill="x", expand=True, ipady=4)
            self._show_tok = False
            self.eye_btn = ttk.Button(arow, text="Show", width=6,
                                      style="Ghost.TButton",
                                      command=self._toggle_tok)
            self.eye_btn.pack(side="left", padx=(6, 0))
            self.remember_var = tk.BooleanVar(value=False)
            remember = ttk.Checkbutton(
                arow, text="Remember on this device",
                variable=self.remember_var)
            remember.pack(side="left", padx=(14, 0))
            ttk.Label(
                main, style="Caption.TLabel",
                text="Optional — unlocks batched GraphQL checks "
                     "(≈1 request per 100 repos) and a 5,000 req/hr budget. "
                     "No scopes are needed for public repos."
            ).pack(anchor="w", pady=(4, 0))

            # ── log ──────────────────────────────────────────
            self._rule(main)
            lhead = ttk.Frame(main)
            lhead.pack(fill="x")
            ttk.Label(lhead, text="ACTIVITY", style="Section.TLabel").pack(side="left")

            box = tk.Frame(main, bg=Palette.BORDER, padx=1, pady=1)
            box.pack(fill="both", expand=True, pady=(6, 0))
            self.log = scrolledtext.ScrolledText(
                box, height=11, state="disabled", wrap="word",
                bg=Palette.SURF_DEEP, fg=Palette.TEXT,
                insertbackground=Palette.TEXT, relief="flat", borderwidth=0,
                padx=10, pady=7, highlightthickness=0)
            self.log.pack(fill="both", expand=True)
            self.log.tag_config("ok", foreground=Palette.GREEN)
            self.log.tag_config("err", foreground=Palette.RED)
            self.log.tag_config("warn", foreground=Palette.YELLOW)
            self.log.tag_config("accent", foreground=Palette.ACCENT)
            self.log.tag_config("info", foreground=Palette.SUBTEXT)
            self.log.tag_config("dim", foreground=Palette.FAINT)

            # ── action bar ───────────────────────────────────
            bar = ttk.Frame(main)
            bar.pack(fill="x", pady=(10, 0))
            ttk.Label(bar, text="Workers:").pack(side="left")
            self.workers_var = tk.IntVar(value=DEFAULT_WORKERS)
            self.workers_spin = ttk.Spinbox(bar, from_=1, to=50, width=3,
                                            textvariable=self.workers_var)
            self.workers_spin.pack(side="left", padx=(6, 12))

            self.stop_btn = ttk.Button(bar, text="■  Stop",
                                       style="Danger.TButton",
                                       command=self._stop, state="disabled")
            self.stop_btn.pack(side="right")
            self.start_btn = ttk.Button(bar, text="Start check",
                                        style="Accent.TButton",
                                        command=self._start)
            self.start_btn.pack(side="right", padx=(0, 8))
            self.html_btn = ttk.Button(bar, text="HTML",
                                       style="Ghost.TButton",
                                       command=self._export_html,
                                       state="disabled")
            self.html_btn.pack(side="right", padx=(0, 8))
            self.open_btn = ttk.Button(bar, text="Open report",
                                       style="Ghost.TButton",
                                       command=self._open_report,
                                       state="disabled")
            self.open_btn.pack(side="right", padx=(0, 8))

            # ── status strip ─────────────────────────────────
            status = tk.Frame(main, bg=Palette.SURFACE)
            status.pack(fill="x", pady=(10, 0))
            self.status_var = tk.StringVar(value="Ready")
            self.status_lbl = tk.Label(status, textvariable=self.status_var,
                                       bg=Palette.SURFACE, fg=Palette.SUBTEXT,
                                       font=("sans-serif", 9), anchor="w")
            self.status_lbl.pack(side="left", padx=(10, 10), pady=5)
            self.prog = ttk.Progressbar(status, mode="determinate")
            self.prog.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=7)
            self.prog_lbl = tk.Label(status, text="0 / 0", bg=Palette.SURFACE,
                                     fg=Palette.SUBTEXT,
                                     font=("sans-serif", 9), width=22,
                                     anchor="e", padx=0)
            self.prog_lbl.pack(side="left", padx=(0, 10))

        def _rule(self, parent):
            line = tk.Frame(parent, bg=Palette.BORDER, height=1)
            line.pack(fill="x", pady=(12, 10))

        # ── drag & drop / paste ──────────────────────────────
        def _setup_drag_drop(self):
            try:
                self.in_entry.drop_target_register("DND_Files")
                self.in_entry.dnd_bind("<<Drop>>", self._on_drop)
                self.tok_entry.drop_target_register("DND_Files")
                self.tok_entry.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass
            for w in (self.in_entry, self.out_entry):
                w.bind("<Button-3>", self._paste_context_menu)

        @staticmethod
        def _paths_from_text(raw: str) -> list[str]:
            from urllib.parse import unquote
            raw = raw.strip()
            if not raw:
                return []
            if raw.startswith("file://"):
                raw = raw[len("file://"):]
            if "{" in raw:
                # Tk DnD-style list: {path one} {path two}
                parts = re.findall(r"\{[^{}]*\}", raw)
            else:
                parts = raw.splitlines() if "\n" in raw or "\r" in raw else [raw]
            out = []
            for p in parts:
                p = p.strip("{}").strip()
                p = unquote(p)
                if p:
                    out.append(p)
            return out

        def _on_drop(self, event):
            if getattr(event, "widget", None) not in (None, self.in_entry):
                return
            paths = self._paths_from_text(getattr(event, "data", ""))
            found = False
            for p in paths:
                path = Path(p)
                if path.is_dir():
                    self.input_files.extend(find_markdown_files(path))
                    found = True
                elif path.is_file():
                    self.input_files.append(str(path.resolve()))
                    found = True
            if found:
                self._update_input_display()

        def _check_clipboard_for_path(self):
            try:
                raw = self.clipboard_get().strip().strip("'\"")
                paths = self._paths_from_text(raw)
            except (tk.TclError, OSError):
                return
            for p in paths:
                path = Path(p)
                if path.is_dir():
                    self.input_files.extend(find_markdown_files(path))
                elif path.is_file():
                    self.input_files.append(str(path.resolve()))
            if paths:
                self._update_input_display()

        def _paste_context_menu(self, event):
            menu = tk.Menu(self, tearoff=0, bg=Palette.SURFACE,
                           fg=Palette.TEXT, activebackground=Palette.ACCENT,
                           activeforeground=Palette.ONACCENT)
            menu.add_command(label="Paste path from clipboard",
                             command=self._check_clipboard_for_path)
            if event.widget is self.in_entry:
                menu.add_command(label="Choose folder…",
                                 command=self._browse_in)
                menu.add_separator()
                menu.add_command(label="Clear input", command=self._clear_input)
            else:
                menu.add_command(label="Choose output file…",
                                 command=self._browse_out)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        def _clear_input(self):
            self.input_files = []
            self.in_var.set("")
            self._update_input_display()

        # ── input / output resolution ────────────────────────
        def _browse_in(self):
            folder = filedialog.askdirectory(
                title="Select folder containing Markdown files")
            if folder:
                found = find_markdown_files(folder)
                if not found:
                    messagebox.showwarning(
                        "No Markdown files",
                        "The selected folder contains no Markdown files.",
                    )
                    return
                self.input_files = found
                self._update_input_display()

        def _update_input_display(self):
            if len(self.input_files) == 1:
                self.in_var.set(self.input_files[0])
                self.src_hint.config(text="1 file")
            elif self.input_files:
                self.in_var.set("")
                self.src_hint.config(
                    text=f"{len(self.input_files)} Markdown files ready to scan")
            else:
                self.src_hint.config(
                    text="Markdown files or a folder containing them")

        def _browse_out(self):
            p = filedialog.asksaveasfilename(
                title="Save report as", defaultextension=".md",
                initialfile=self.out_var.get() or "freshness_report.md",
                filetypes=[("Markdown report", "*.md"),
                           ("HTML report", "*.html")])
            if p:
                self.out_var.set(p)

        def _resolve_inputs(self) -> tuple[list[str], str]:
            """(input files, error message or '')."""
            if self.input_files:
                return self.input_files, ""
            typed = self.in_var.get().strip()
            if not typed:
                return [], "Choose or drop the files that list the repositories."
            p = Path(typed)
            if p.is_dir():
                found = find_markdown_files(p)
                if not found:
                    return [], f"No Markdown files found in “{typed}”."
                return found, ""
            if p.is_file():
                return [str(p.resolve())], ""
            return [], f"Path not found: “{typed}”."

        # ── token actions ────────────────────────────────────
        def _toggle_tok(self):
            self._show_tok = not self._show_tok
            self.tok_entry.config(show="" if self._show_tok else "●")
            self.eye_btn.config(text="Hide" if self._show_tok else "Show")

        def _resolve_token(self) -> tuple[str, str]:
            """(token, error message or '')."""
            tok = self.tok_var.get().strip()
            if not tok:
                tok = load_token()
                if tok:
                    self.tok_var.set(tok)
                    self.remember_var.set(True)
            if tok:
                is_valid, msg = validate_token(tok)
                if not is_valid:
                    return "", f"{msg}\n\nYou can still continue — paste a valid token, or run without one (60 req/hr)."
            return tok, ""

        # ── run lifecycle ────────────────────────────────────
        def _start(self):
            if self.running:
                return
            inp, err = self._resolve_inputs()
            if err:
                messagebox.showerror("Input", err)
                return
            out = self.out_var.get().strip()
            if not out:
                messagebox.showerror("Output", "Choose a file to save the report to.")
                return
            tok, tok_err = self._resolve_token()
            if tok_err:
                if messagebox.askyesno("Token looks invalid", tok_err):
                    tok = ""   # run unauthenticated instead
                else:
                    return
            elif not tok and not messagebox.askyesno(
                    "Continue without a token?",
                    "GitHub allows only 60 requests/hour without one, so "
                    "checking large lists can take a long time. A token "
                    "also enables batched GraphQL checks.\n\n"
                    "Continue without a token?"):
                return

            # token persistence follows the checkbox
            try:
                if tok:
                    if self.remember_var.get():
                        save_token(tok)
                    else:
                        clear_saved_token()
            except OSError as exc:
                messagebox.showwarning("Token", f"Could not save the token:\n{exc}")

            self.running = True
            self.cancel_event.clear()
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.open_btn.config(state="disabled")
            self.html_btn.config(state="disabled")
            self.workers_spin.config(state="disabled")
            self.prog["value"] = 0
            self._log_clear()
            self._status("Extracting repository URLs…", Palette.SUBTEXT)

            t = threading.Thread(target=self._worker,
                                 args=(inp, out, tok), daemon=True)
            t.start()

        def _stop(self):
            self.cancel_event.set()
            self.stop_btn.config(state="disabled")
            self._status("Stopping — waiting for the current request…",
                         Palette.YELLOW)

        def _worker(self, inp, out, tok):
            try:
                self._safe_log(f"Reading {len(inp)} input file(s)…", "info")
                repos = extract_repos(expand_input_paths(inp))
                self._safe_log(f"Found {len(repos)} unique repositories", "accent")
                if not repos:
                    self._safe_status("No repositories found in the input.")
                    self._finish()
                    return

                workers = self.workers_var.get()
                self._safe_log(f"Workers: {workers}", "dim")
                self._safe_status("Checking repositories…", Palette.SUBTEXT)
                results, errors = process_repos(
                    repos, tok,
                    progress_cb=self._safe_progress,
                    log_cb=self._safe_log,
                    cancel=self.cancel_event,
                    max_workers=workers,
                )

                self.after(0, lambda: setattr(self, "last_results", results))
                self.after(0, lambda: setattr(self, "last_errors", errors))

                stopped = self.cancel_event.is_set()
                if results or errors:
                    generate_report(results, errors, out)
                    html_path = str(Path(out).with_suffix(".html"))
                    generate_html_report(results, errors, html_path)
                    self._safe_log("", "dim")
                    if stopped:
                        self._safe_log("Stopped by user — writing partial "
                                       "results…", "warn")
                    self._safe_log(f"Markdown report → {out}", "ok")
                    self._safe_log(f"HTML report     → {html_path}", "ok")
                    self._safe_log(f"{len(results)} checked  ·  "
                                   f"{len(errors)} errors", "dim")
                    if stopped:
                        self._safe_status("Stopped — partial results saved",
                                          Palette.YELLOW)
                    else:
                        self._safe_status("Done — report saved", Palette.GREEN)
                    self.after(0, lambda: self.open_btn.config(state="normal"))
                    self.after(0, lambda: self.html_btn.config(state="normal"))
                else:
                    self._safe_status("Nothing to report.", Palette.YELLOW)
            except Exception as exc:
                self._safe_log(f"Unexpected error: {exc}", "err")
                self._safe_status("Failed — see activity log", Palette.RED)
            finally:
                self._finish()
                self.after(0, self._save_config)

        def _finish(self):
            self.running = False
            self.after(0, lambda: self.start_btn.config(state="normal"))
            self.after(0, lambda: self.stop_btn.config(state="disabled"))
            self.after(0, lambda: self.workers_spin.config(state="normal"))

        # ── report actions ───────────────────────────────────
        def _open_path(self, path: str):
            import subprocess, platform
            if not Path(path).exists():
                return False
            s = platform.system()
            try:
                if s == "Darwin":
                    subprocess.Popen(["open", path])
                elif s == "Windows":
                    os.startfile(path)
                else:
                    subprocess.Popen(["xdg-open", path])
            except OSError:
                return False
            return True

        def _open_report(self):
            p = self.out_var.get().strip()
            if p and not self._open_path(p):
                messagebox.showerror("Open",
                                     "The report does not exist yet — run a check first.")
            elif not p:
                messagebox.showerror("Open", "Choose an output path first.")

        def _export_html(self):
            md_path = self.out_var.get().strip()
            html_path = str(Path(md_path or "freshness_report.md").with_suffix(".html"))
            if not Path(html_path).exists():
                results = getattr(self, "last_results", None)
                errors = getattr(self, "last_errors", None)
                if results is None or errors is None:
                    messagebox.showerror(
                        "Export", "No report data available — run a check first.")
                    return
                try:
                    generate_html_report(results, errors, html_path)
                except OSError as exc:
                    messagebox.showerror("Export", f"Could not write the file:\n{exc}")
                    return
            if not self._open_path(html_path):
                messagebox.showerror("Export",
                                     f"The HTML report was written to:\n{html_path}")

        # ── logging / progress ───────────────────────────────
        def _log_clear(self):
            self.log.config(state="normal")
            self.log.delete("1.0", "end")
            self.log.config(state="disabled")

        def _sanitize(self, text: str) -> str:
            token = self.tok_var.get().strip()
            if token and token in text:
                text = text.replace(token, "***")
            return text

        def _log(self, msg: str, tag: str | None = None):
            msg = self._sanitize(msg)
            self.log.config(state="normal")
            if msg:
                if msg.startswith("✅") or msg.startswith("📝") or msg.startswith("🌐"):
                    tag = "ok"
                elif msg.startswith("❌") or msg.startswith("💥"):
                    tag = "err"
                elif msg.startswith("⏳") or msg.startswith("⚠"):
                    tag = "warn"
                elif msg.startswith("🔑") or msg.startswith("⚙"):
                    tag = "accent"
                elif msg.startswith("["):
                    tag = tag or "info"
            self.log.insert("end", msg + "\n" if msg else "\n",
                             tag or "info")
            self.log.see("end")
            self.log.config(state="disabled")
            if tag == "err":
                self._status("Error — see activity log", Palette.RED)

        def _status(self, text: str, color: str | None = None):
            self.status_var.set(self._sanitize(text))
            self.status_lbl.config(fg=color or Palette.SUBTEXT)

        def _progress(self, cur, total, dyn_info=None):
            pct = cur / total * 100 if total else 0
            self.prog["value"] = pct
            label = f"{cur} / {total}"
            if dyn_info and dyn_info.get("eta", 0) > 0:
                eta = dyn_info["eta"]
                if eta >= 3600:
                    label += f"  ·  ETA {eta / 3600:.1f}h"
                elif eta >= 60:
                    label += f"  ·  ETA {eta / 60:.0f}m"
                else:
                    label += f"  ·  ETA {eta:.0f}s"
            self.prog_lbl.config(text=label)

        # thread-safe marshalling
        def _safe_log(self, msg, tag=None):
            self.after(0, lambda m=msg, t=tag: self._log(m, t))

        def _safe_progress(self, cur, total, dyn_info=None):
            self.after(0, lambda: self._progress(cur, total, dyn_info))

        def _safe_status(self, msg, color=None):
            self.after(0, lambda: self._status(msg, color))

        # ── config persistence ───────────────────────────────
        def _load_config(self):
            try:
                if not self.CONFIG_FILE.exists():
                    return
                data = json.loads(self.CONFIG_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return
            saved_in = data.get("last_input", "")
            if isinstance(saved_in, list) and saved_in:
                alive = [s for s in saved_in if Path(s).is_file()]
                self.input_files = alive
                self._update_input_display()
            elif isinstance(saved_in, str) and saved_in:
                self.in_var.set(saved_in)
            if data.get("last_output"):
                self.out_var.set(data["last_output"])
            try:
                if data.get("last_workers"):
                    self.workers_var.set(int(data["last_workers"]))
            except (TypeError, ValueError):
                pass

        def _save_config(self):
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                data = {
                    "last_input": list(self.input_files)
                                  or [self.in_var.get().strip()],
                    "last_output": self.out_var.get().strip(),
                    "last_workers": self.workers_var.get(),
                }
                self.CONFIG_FILE.write_text(json.dumps(data, indent=2),
                                            encoding="utf-8")
            except OSError:
                pass

    app = App()
    app.mainloop()


# ═══════════════════════════ CLI ════════════════════════════════
def run_cli():
    ap = argparse.ArgumentParser(
        description="Check GitHub repo freshness and generate a Markdown report."
    )
    ap.add_argument("input_file", nargs="+", help="One or more files or folders containing GitHub URLs")
    ap.add_argument("-o", "--output", default="freshness_report.md",
                    help="Output path  (default: freshness_report.md)")
    ap.add_argument("-f", "--format", choices=["md", "html", "both"],
                    default="md",
                    help="Output format(s): md (default), html, or both")
    ap.add_argument("-t", "--token", default=None,
                    help="GitHub Personal Access Token")
    ap.add_argument("--save-token", action="store_true",
                    help="Save the supplied token for future runs")
    ap.add_argument("-w", "--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"Concurrent API workers (default: {DEFAULT_WORKERS})")
    args = ap.parse_args()

    token = args.token or load_token()
    if args.save_token and args.token:
        save_token(args.token)
        print(f"✓ Token saved to {TOKEN_FILE}")

    if not token:
        print("⚠  No token - rate limit is 60 req/hr.  "
              "Pass --token or set GITHUB_TOKEN.")

    expanded = expand_input_paths(args.input_file)
    repos = extract_repos(expanded)
    print(f"🔍  Found {len(repos)} unique GitHub repos in {len(expanded)} input file(s)")
    if not repos:
        sys.exit(0)

    print(f"⚙  Using {args.workers} concurrent workers")
    start = time.time()

    def _cli_progress(cur, total, dyn_info=None):
        pct = cur / total * 100 if total else 0
        label = f"  Progress: {cur}/{total}  ({pct:.1f}%)"
        if dyn_info and dyn_info.get("eta", 0) > 0:
            eta = dyn_info["eta"]
            if eta >= 3600:
                label += f"  ETA: {eta/3600:.1f}h"
            elif eta >= 60:
                label += f"  ETA: {eta/60:.0f}m"
            else:
                label += f"  ETA: {eta:.0f}s"
        print(f"\r{label:<72}", end="", flush=True)

    results, errors = process_repos(
        repos, token,
        progress_cb=_cli_progress,
        log_cb=lambda m: print(f"\r{m:<80}"),
        max_workers=args.workers,
    )
    elapsed = time.time() - start
    print()

    generate_report(results, errors, args.output)
    print(f"\n📝  Report saved → {args.output}")

    if args.format in ("html", "both"):
        html_path = str(Path(args.output).with_suffix('.html'))
        generate_html_report(results, errors, html_path)
        print(f"🌐  HTML report  → {html_path}")

    print(f"    ✅ {len(results)} repos  |  ❌ {len(errors)} errors  "
          f"|  ⏱ {elapsed:.1f}s")

# ════════════════════════ entry point ═══════════════════════════
if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--gui"):
        run_cli()
    else:
        run_gui()