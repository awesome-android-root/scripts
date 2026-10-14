#!/usr/bin/env python3
"""
GitHub Repo Freshness Checker
============================
Reads files containing GitHub URLs, checks each repository's last push via
the GitHub API, and writes a sorted Markdown and/or HTML report.  Requests
run in parallel (ThreadPoolExecutor), and the token is validated against the
API before any checking starts.

  GUI mode : python repo_freshness_checker.py
  CLI mode : python repo_freshness_checker.py README.md -o report.md -t ghp_xxx

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
VERSION           = "2.4.0"
CONFIG_DIR        = Path.home() / ".repo-freshness-checker"
TOKEN_FILE        = CONFIG_DIR / "token"
API_BASE          = "https://api.github.com"
DEFAULT_WORKERS   = 15       # concurrent API requests
REQUEST_TIMEOUT   = 15       # seconds per request
MAX_RETRIES       = 2        # max retries on rate-limit

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


def check_token(token: str, timeout: int = 10) -> dict:
    """Validate a token against the GitHub API (GET /user).

    Returns {"ok", "level", "message", "login", "limit", "remaining"} where
    level is "ok" | "warn" | "error".  An empty token is not an error: the
    tool works unauthenticated, just with a 60 requests/hour limit.
    """
    token = (token or "").strip()
    if not token:
        return {"ok": False, "level": "warn", "login": None, "limit": 60,
                "remaining": None,
                "message": "No token - unauthenticated (60 requests/hour)"}

    session = _session(token)
    try:
        r = session.get(f"{API_BASE}/user", timeout=timeout)
    except _requests.exceptions.Timeout:
        return {"ok": False, "level": "error", "login": None, "limit": None,
                "remaining": None,
                "message": "Timed out talking to api.github.com"}
    except _requests.exceptions.RequestException as exc:
        return {"ok": False, "level": "error", "login": None, "limit": None,
                "remaining": None,
                "message": f"Cannot reach GitHub: {exc}"}

    def _int(header):
        try:
            return int(r.headers[header])
        except (KeyError, ValueError):
            return None

    info = {"ok": False, "level": "error", "login": None,
            "limit": _int("X-RateLimit-Limit"),
            "remaining": _int("X-RateLimit-Remaining"),
            "message": ""}

    if r.status_code == 200:
        info.update(ok=True, level="ok", login=r.json().get("login") or "?",
                    message=f"Valid - authenticated as "
                            f"{r.json().get('login') or '?'}")
    elif r.status_code == 401:
        info["message"] = ("Token rejected by GitHub - invalid, expired "
                           "or revoked")
    elif r.status_code == 403:
        if info["remaining"] == 0:
            info["level"] = "warn"
            info["message"] = ("Token may be valid, but its rate limit is "
                               "exhausted")
        else:
            info["message"] = ("Forbidden. For an org/SSO token, authorise "
                               "it at https://github.com/settings/tokens")
    else:
        info["message"] = f"GitHub returned HTTP {r.status_code}"

    return info

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
def _session(token: str) -> _requests.Session:
    s = _requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github.v3+json",
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

def fetch_repo(session: _requests.Session, owner: str, repo: str) -> dict:
    """Fetch a single repo's info. Returns a dict with consistent keys."""
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
        if r.status_code == 403:
            rem = r.headers.get("X-RateLimit-Remaining", "?")
            if rem == "0":
                reset = int(r.headers.get("X-RateLimit-Reset", 0))
                return {"ok": False, "error": "Rate-limited",
                        "rate_limited": True, "reset_ts": reset}
            return {"ok": False, "error": "Forbidden (403)"}
        if r.status_code == 429:
            reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
            return {"ok": False, "error": "Rate-limited (429)",
                    "rate_limited": True, "reset_ts": reset}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except _requests.exceptions.Timeout:
        return {"ok": False, "error": "Timeout"}
    except _requests.exceptions.ConnectionError:
        return {"ok": False, "error": "Connection error"}
    except _requests.exceptions.RequestException as e:
        return {"ok": False, "error": f"Request failed: {e}"}

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
    Check all repos using a thread pool for parallel API requests.
    Returns (results, errors).
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

    # Track remaining rate limit across threads
    rl_remaining = auth["remaining"] if auth else None
    rl_reset_ts = auth["reset_ts"] if auth else 0
    rl_lock = threading.Lock()

    def _check_one(owner: str, repo: str) -> dict | None:
        """Check one repo, retrying on rate-limit. Returns None if cancelled."""
        nonlocal rl_remaining, rl_reset_ts

        if cancel and cancel.is_set():
            return None

        # Pre-check rate limit (approximate, shared across threads)
        with rl_lock:
            if rl_remaining is not None and rl_remaining < 5:
                wait = max(0, rl_reset_ts - time.time()) + 5
                if wait > 0:
                    if log_cb:
                        log_cb(f"⏳ Rate-limit low - waiting {wait:.0f}s …")
                    _interruptible_sleep(wait, cancel)
                    if cancel and cancel.is_set():
                        return None
                    # Re-check auth after wait
                    new_auth = verify_auth(session)
                    if new_auth:
                        rl_remaining = new_auth["remaining"]
                        rl_reset_ts = new_auth["reset_ts"]

        for attempt in range(MAX_RETRIES):
            if cancel and cancel.is_set():
                return None

            info = fetch_repo(session, owner, repo)

            # Decrement remaining counter
            with rl_lock:
                if rl_remaining is not None:
                    rl_remaining -= 1

            # Handle rate-limit with retry
            if info.get("rate_limited"):
                wait = max(0, info.get("reset_ts", time.time() + 60)) - time.time() + 5
                if log_cb:
                    log_cb(f"⏳ Rate-limited - waiting {wait:.0f}s …")
                _interruptible_sleep(wait, cancel)
                if cancel and cancel.is_set():
                    return None
                # Re-check auth after wait
                new_auth = verify_auth(session)
                if new_auth:
                    rl_remaining = new_auth["remaining"]
                    rl_reset_ts = new_auth["reset_ts"]
                continue

            return (owner, repo, info)

        # All retries exhausted
        return (owner, repo, {"ok": False, "error": "Rate-limited (retries exhausted)"})

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_check_one, owner, repo): (owner, repo)
            for owner, repo in repos
        }

        for future in as_completed(future_map):
            if cancel and cancel.is_set():
                # Cancel remaining futures
                for f in future_map:
                    f.cancel()
                if log_cb:
                    log_cb("⛔ Cancelled.")
                break

            result = future.result()
            if result is None:
                continue

            owner, repo, info = result

            with processed_lock:
                processed += 1
                cur = processed

            # ETA calculation
            elapsed = time.time() - start_time
            rate = cur / elapsed if elapsed > 0 else 0
            eta_secs = (total - cur) / rate if rate > 0 else 0

            dyn_info = {"cur": cur, "total": total, "eta": eta_secs, "rate": rate}

            if progress_cb:
                progress_cb(cur, total, dyn_info)

            if info["ok"]:
                ds = info["pushed_at"].strftime("%Y-%m-%d") if info["pushed_at"] else "N/A"
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
# (key, label, range) - one entry per freshness bucket, in report order.
STATUS_BUCKETS: list[tuple[str, str, str]] = [
    ("active",   "🟢 Active",  "< 1 yr"),
    ("aging",    "🟡 Aging",   "1-3 yrs"),
    ("old",      "🟠 Old",     "3-5 yrs"),
    ("stale",    "🔴 Stale",   "5+ yrs"),
    ("archived", "📦 Archived", "read-only"),
]
_BUCKET_LABEL = {key: label for key, label, _ in STATUS_BUCKETS}
_HEALTH_BY_RATIO = ((0.9, "🟢", "Excellent"), (0.7, "🟡", "Fair"),
                    (0.4, "🟠", "Poor"))


def _age_str(days: int) -> str:
    if days <= 0:
        return "today"
    y, rem = divmod(days, 365)
    m = rem // 30
    if y:
        return f"{y}y {m}m" if m else f"{y}y"
    if m:
        return f"{m}m"
    return f"{days}d"


def _age_days(r: dict, now: datetime) -> int | None:
    if not r.get("date"):
        return None
    return max(0, (now - r["date"]).days)


def _bucket(r: dict, now: datetime) -> str:
    if r["archived"]:
        return "archived"
    d = _age_days(r, now)
    if d is None:
        return "archived"
    if d > 5 * 365:
        return "stale"
    if d > 3 * 365:
        return "old"
    if d > 365:
        return "aging"
    return "active"


def _status(days: int, archived: bool) -> str:
    """Human-readable status for a repo of the given age."""
    if archived:
        return _BUCKET_LABEL["archived"]
    if days > 5 * 365:
        return _BUCKET_LABEL["stale"]
    if days > 3 * 365:
        return _BUCKET_LABEL["old"]
    if days > 365:
        return _BUCKET_LABEL["aging"]
    return _BUCKET_LABEL["active"]


def _summarize(results: list[dict], errors: list[dict],
               now: datetime) -> dict:
    """Derive every number both report formats need.

    Callers may rely on *results* already being sorted oldest-first.
    """
    counts = {key: 0 for key, _, _ in STATUS_BUCKETS}
    dated: list[dict] = []
    for r in results:
        counts[_bucket(r, now)] += 1
        if r.get("date"):
            dated.append(r)
    dated.sort(key=lambda r: r["date"])

    # Health: share of non-archived repos updated within the last year.
    live = len(results) - counts["archived"]
    fresh = sum(1 for r in dated
                if not r["archived"] and _age_days(r, now) <= 365)
    ratio = fresh / live if live else 0
    if live:
        emoji, label = next(((e, l) for t, e, l in _HEALTH_BY_RATIO
                             if ratio >= t), ("🔴", "Critical"))
    else:
        emoji, label = "❓", "No data"

    ages = sorted(_age_days(r, now) for r in dated if _age_days(r, now))
    median = ages[len(ages) // 2] if ages else None

    return {
        "counts": counts,
        "total_ok": len(results),
        "total_err": len(errors),
        "total": len(results) + len(errors),
        "stars": sum(r.get("stars", 0) for r in results),
        "oldest": dated[0] if dated else None,
        "newest": dated[-1] if dated else None,
        "median_days": median,
        "fresh": fresh,
        "live": live,
        "ratio": ratio,
        "health": f"{emoji} {label}",
        "health_emoji": emoji,
        "health_label": label,
        "error_kinds": _group_errors(errors),
    }


def _group_errors(errors: list[dict]) -> dict[str, int]:
    """Count errors by kind so the report can summarise the failures."""
    kinds: dict[str, int] = {}
    for e in errors:
        kinds[e["error"]] = kinds.get(e["error"], 0) + 1
    return dict(sorted(kinds.items(), key=lambda kv: -kv[1]))


def _health_score(results: list[dict]) -> str:
    """Overall health emoji based on the distribution of results."""
    if not results:
        return "❓"
    now = datetime.now(timezone.utc)
    return _summarize(results, [], now)["health"]


def generate_report(results: list[dict], errors: list[dict], path: str):
    """Write a Markdown report, sorted oldest commit first."""
    now = datetime.now(timezone.utc)
    results.sort(key=lambda r: r["date"] or datetime.min.replace(tzinfo=timezone.utc))
    s = _summarize(results, errors, now)

    L: list[str] = []
    L.append("# 📊 Repository Freshness\n")
    L.append(f"_{now.strftime('%Y-%m-%d %H:%M')} UTC · "
             f"{s['total']} repositories checked_\n")

    # ── headline ────────────────────────────────────────────────
    share = f"{s['ratio']:.0%}"
    L.append(f"**{s['health']}** — {share} of live repos were "
             f"updated within the past year "
             f"({s['fresh']} of {s['live']}).\n")

    # ── distribution ─────────────────────────────────────────────
    L.append("| Status | Range | Count | Share |")
    L.append("|:--|:--|--:|--:|")
    for key, label, rng in STATUS_BUCKETS:
        n = s["counts"][key]
        pct = f"{n / s['total_ok']:.1%}" if s["total_ok"] else "–"
        L.append(f"| {label} | {rng} | {n} | {pct} |")
    L.append(f"| **Total reachable** | | **{s['total_ok']}** | |")
    if errors:
        L.append(f"| ⚠️ Unreachable | 404 / 403 / network | {s['total_err']} | |")
    L.append("")

    # ── supporting numbers ───────────────────────────────────────
    facts = [f"⭐ **{s['stars']:,}** stars in total"]
    if s["oldest"]:
        facts.append(f"oldest commit {_age_str(_age_days(s['oldest'], now))} ago "
                     f"(`{s['oldest']['date']:%Y-%m-%d}`)")
    if s["median_days"] is not None:
        facts.append(f"median age **{_age_str(s['median_days'])}**")
    if s["newest"]:
        facts.append(f"newest commit `{s['newest']['date']:%Y-%m-%d}`")
    L.append(" · ".join(facts) + "\n")

    # ── table ────────────────────────────────────────────────────
    L.append("## Repositories\n")
    L.append(f"Oldest first. {s['total_ok']} of {s['total']} reachable.\n")
    L.append("| # | Repository | Last commit | Age | ⭐ | Status |")
    L.append("|--:|:--|:--|--:|--:|:--|")
    for i, r in enumerate(results, 1):
        d = _age_days(r, now)
        if d is None:
            ds, age, st = "n/a", "–", "❓"
        else:
            ds, age = f"{r['date']:%Y-%m-%d}", _age_str(d)
            st = _status(d, r["archived"])
        L.append(f"| {i} | [{r['name']}]({r['url']}) | {ds} | {age} | "
                 f"{r['stars']:,} | {st} |")
    L.append("")

    # ── errors ───────────────────────────────────────────────────
    if errors:
        L.append("## Unreachable\n")
        summary = ", ".join(f"{n}× {kind}"
                            for kind, n in s["error_kinds"].items())
        L.append(f"{len(errors)} of {s['total']} repositories could not be "
                 f"checked — {summary}.\n")
        L.append("| # | Repository | Error |")
        L.append("|--:|:--|:--|")
        for i, e in enumerate(errors, 1):
            L.append(f"| {i} | [{e['name']}]({e['url']}) | {e['error']} |")
        L.append("")

    L.append("---")
    L.append(f"<sub>Repo Freshness Checker v{VERSION} · "
             f"buckets: 🟢 &lt;1 yr · 🟡 1-3 yrs · 🟠 3-5 yrs · "
             f"🔴 &gt;5 yrs · 📦 read-only</sub>")

    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


# ──────────────────── HTML report ──────────────────────────────
_CSS = """
:root{--bg:#11111b;--surface:#1a1a26;--raised:#232331;--line:#2f2f40;
 --text:#d7d9e0;--dim:#8b8fa3;--faint:#5f6377;--link:#7aa2f7;
 --active:#7ec98f;--aging:#e0c274;--old:#dd9268;--stale:#e5748a;
 --archived:#8b8fa3;--warn:#e0c274;--err:#e5748a}
html[data-light="true"]{--bg:#f6f7f9;--surface:#fff;--raised:#f0f1f5;--line:#e2e4ea;
 --text:#1c1e26;--dim:#5b6070;--faint:#8a8fa0;--link:#2f6fd0;
 --active:#1f7a3d;--aging:#8a6a12;--old:#9a5a24;--stale:#a3304a;
 --archived:#5b6070;--warn:#8a6a12;--err:#a3304a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);line-height:1.55;
 font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,sans-serif;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:32px 24px 64px}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}

header{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:24px}
h1{font-size:20px;font-weight:650;margin:0;letter-spacing:-.01em}
h2{font-size:15px;font-weight:600;margin:32px 0 12px;letter-spacing:-.005em}
.sub{color:var(--dim);font-size:13px;margin-top:4px}
.spacer{flex:1}

.health{display:inline-flex;align-items:center;gap:8px;padding:7px 14px;
 border-radius:999px;font-size:13px;font-weight:600;background:var(--raised);
 border:1px solid var(--line);white-space:nowrap}
.health b{font-size:15px}

button,.btn{font:inherit;color:var(--text);background:var(--raised);
 border:1px solid var(--line);border-radius:8px;padding:6px 12px;cursor:pointer}
button:hover{background:var(--line)}
input,select{font:inherit;color:var(--text);background:var(--raised);
 border:1px solid var(--line);border-radius:8px;padding:6px 10px}
input:focus,select:focus{outline:2px solid var(--link);outline-offset:-1px}
input::placeholder{color:var(--faint)}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
 gap:10px;margin-bottom:24px}
.tile{background:var(--surface);border:1px solid var(--line);
 border-radius:10px;padding:12px 14px}
.tile dt{color:var(--dim);font-size:12px;margin:0 0 2px}
.tile dd{margin:0;font-size:19px;font-weight:650;letter-spacing:-.01em;
 font-variant-numeric:tabular-nums}
.tile .unit{font-size:12px;color:var(--dim);font-weight:400}

.bar{display:flex;height:8px;border-radius:999px;overflow:hidden;
 background:var(--raised);margin:0 0 20px}
.bar i{display:block;height:100%}
.bar .active{background:var(--active)}.bar .aging{background:var(--aging)}
.bar .old{background:var(--old)}.bar .stale{background:var(--stale)}
.bar .archived{background:var(--archived)}

.legend{display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--dim);
 font-size:12.5px;margin-bottom:16px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
.dot.active{background:var(--active)}.dot.aging{background:var(--aging)}
.dot.old{background:var(--old)}.dot.stale{background:var(--stale)}
.dot.archived{background:var(--archived)}

.bar-thead{position:sticky;top:0;z-index:2}
table{width:100%;border-collapse:separate;border-spacing:0;background:var(--surface);
 border:1px solid var(--line);border-radius:10px;overflow:hidden}
th{background:var(--raised);text-align:left;font-size:12px;font-weight:600;
 color:var(--dim);text-transform:uppercase;letter-spacing:.04em;padding:9px 12px;
 border-bottom:1px solid var(--line);white-space:nowrap}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--text)}
th .arrow{color:var(--link);margin-left:4px;opacity:0}
th[aria-sort] .arrow{opacity:1}
td{padding:8px 12px;border-bottom:1px solid var(--line);font-size:14px}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--raised)}
.r{text-align:right;font-variant-numeric:tabular-nums}
.muted{color:var(--faint)}
.tag{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;
 padding:2px 9px;border-radius:999px;border:1px solid transparent;white-space:nowrap}
.tag.active{color:var(--active);border-color:color-mix(in srgb,var(--active) 35%,transparent)}
.tag.aging{color:var(--aging);border-color:color-mix(in srgb,var(--aging) 35%,transparent)}
.tag.old{color:var(--old);border-color:color-mix(in srgb,var(--old) 35%,transparent)}
.tag.stale{color:var(--stale);border-color:color-mix(in srgb,var(--stale) 35%,transparent)}
.tag.archived{color:var(--archived);border-color:var(--line)}

.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.toolbar input[type=search]{flex:1;min-width:180px}
.count{color:var(--dim);font-size:12.5px;margin-left:auto;
 font-variant-numeric:tabular-nums}
.empty{text-align:center;padding:40px;color:var(--faint)}
.err-kind{color:var(--err);font-size:13px}
footer{margin-top:36px;padding-top:14px;border-top:1px solid var(--line);
 color:var(--faint);font-size:12px}
@media(max-width:640px){.wrap{padding:20px 14px 48px}.num,.age{display:none}}
"""

_JS = """
const rows = window.__ROWS__;
let sortKey = 'date', dir = 1;

const $ = id => document.getElementById(id);
const tbody = $('body');

function visible() {
  const q = $('search').value.trim().toLowerCase();
  const f = $('filter').value;
  return rows.filter(r => (f === 'all' || r.bucket === f)
    && (!q || r.name.toLowerCase().includes(q) || r.date.includes(q)));
}

function render() {
  const list = visible().slice().sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    return (x < y ? -1 : x > y ? 1 : 0) * dir;
  });

  $('count').textContent = list.length === rows.length
    ? list.length + ' repositories'
    : list.length + ' of ' + rows.length;

  tbody.innerHTML = list.length ? list.map(r => `
    <tr>
      <td class="num muted">${r.num}</td>
      <td><a href="${r.url}" target="_blank" rel="noopener">${r.name}</a></td>
      <td class="r muted">${r.date}</td>
      <td class="r age">${r.age}</td>
      <td class="r muted">${r.stars.toLocaleString()}</td>
      <td><span class="tag ${r.bucket}">${r.label}</span></td>
    </tr>`).join('')
    : '<tr><td colspan="6" class="empty">Nothing matches these filters.</td></tr>';

  document.querySelectorAll('th[data-key]').forEach(th => {
    if (th.dataset.key === sortKey) th.setAttribute('aria-sort', dir > 0 ? 'ascending' : 'descending');
    else th.removeAttribute('aria-sort');
  });
}

document.querySelectorAll('th[data-key]').forEach(th => {
  th.classList.add('sortable');
  th.insertAdjacentHTML('beforeend', '<span class="arrow">\\u25B2</span>');
  th.addEventListener('click', () => {
    const key = th.dataset.key;
    dir = key === sortKey ? -dir : 1;
    sortKey = key;
    render();
  });
});

$('search').addEventListener('input', render);
$('filter').addEventListener('change', render);

document.querySelectorAll('[data-filter]').forEach(el => el.addEventListener('click', () => {
  $('filter').value = el.dataset.filter;
  $('search').value = '';
  render();
  $('repos').scrollIntoView({ behavior: 'smooth', block: 'start' });
}));

$('theme').addEventListener('click', () => {
  const light = document.documentElement.dataset.light === 'true';
  document.documentElement.dataset.light = String(!light);
  localStorage.setItem('rf-theme', light ? 'dark' : 'light');
  $('theme').textContent = light ? 'Light' : 'Dark';
});

const saved = localStorage.getItem('rf-theme');
if (saved === 'light') {
  document.documentElement.dataset.light = 'true';
  $('theme').textContent = 'Dark';
}

render();
"""


def generate_html_report(results: list[dict], errors: list[dict], path: str):
    """Write a standalone HTML report (sorting/filtering, no dependencies)."""
    import html as _html

    now = datetime.now(timezone.utc)
    results.sort(key=lambda r: r["date"] or datetime.min.replace(tzinfo=timezone.utc))
    s = _summarize(results, errors, now)

    # ── rows for the client-side table ────────────────────────────
    rows_data = []
    for i, r in enumerate(results, 1):
        d = _age_days(r, now)
        bucket = _bucket(r, now)
        rows_data.append({
            "num": i,
            "name": _html.escape(r["name"]),
            "url": r["url"],
            "date": f"{r['date']:%Y-%m-%d}" if d is not None else "",
            "age": _age_str(d) if d is not None else "–",
            "days": d if d is not None else -1,   # numeric, for sorting
            "stars": r.get("stars", 0),
            "bucket": bucket,
            "label": _BUCKET_LABEL[bucket],
        })
    rows_json = json.dumps(rows_data, ensure_ascii=False).replace("</", "<\\/")

    # ── distribution bar ──────────────────────────────────────────
    bar = "".join(
        f'<i class="{key}" style="width:{s["counts"][key] / s["total_ok"] * 100:.4f}%"></i>'
        for key, _, _ in STATUS_BUCKETS if s["counts"][key]
    ) if s["total_ok"] else ""

    legend = "".join(
        f'<span><span class="dot {key}"></span>{label} {rng}</span>'
        for key, label, rng in STATUS_BUCKETS
    )

    # ── stat tiles ────────────────────────────────────────────────
    def _tile(label, value, unit=""):
        unit_html = f' <span class="unit">{unit}</span>' if unit else ""
        return (f'<div class="tile"><dt>{label}</dt>'
                f'<dd>{value}{unit_html}</dd></div>')

    tiles = [
        _tile("Reachable", f"{s['total_ok']:,}",
              f"of {s['total']:,}" if s["total_err"] else ""),
        _tile("Updated &lt; 1 yr", f"{s['ratio']:.0%}"
              if s["live"] else "–", "of live repos"),
        _tile("Stars", f"{s['stars']:,}"),
        _tile("Median age", _age_str(s["median_days"])
              if s["median_days"] is not None else "–"),
        _tile("Unreachable", f"{s['total_err']:,}" if s["total_err"] else "0"),
    ]

    err_section = ""
    if errors:
        err_rows = "".join(
            f'<tr><td class="num muted">{i}</td>'
            f'<td><a href="{_html.escape(e["url"])}" target="_blank" '
            f'rel="noopener">{_html.escape(e["name"])}</a></td>'
            f'<td class="err-kind">{_html.escape(e["error"])}</td></tr>'
            for i, e in enumerate(errors, 1)
        )
        kinds = ", ".join(f"{n}× {_html.escape(k)}"
                          for k, n in s["error_kinds"].items())
        err_section = (
            f'<h2>Unreachable ({len(errors)})</h2>'
            f'<p class="sub">{kinds}</p>'
            f'<table><thead class="bar-thead"><tr><th class="num">#</th>'
            f'<th>Repository</th><th>Error</th></tr></thead>'
            f'<tbody>{err_rows}</tbody></table>'
        )

    HTML = f"""<!DOCTYPE html>
<html lang="en" data-light="false">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Repository Freshness · {now:%Y-%m-%d}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1>Repository Freshness</h1>
    <div class="sub">{s['total']:,} repositories · generated {now:%Y-%m-%d %H:%M} UTC</div>
  </div>
  <div class="spacer"></div>
  <span class="health"><b>{s['health_emoji']}</b> {s['health_label']}</span>
  <button id="theme" title="Toggle light / dark">Light</button>
</header>

<dl class="tiles">{''.join(tiles)}</dl>

<div class="bar">{bar}</div>
<div class="legend">{legend}</div>

<h2 id="repos">Repositories</h2>
<div class="toolbar">
  <input type="search" id="search" placeholder="Filter by name or date…"
         autocomplete="off" autofocus>
  <select id="filter">
    <option value="all">All statuses</option>
    <option value="active">🟢 Active</option>
    <option value="aging">🟡 Aging</option>
    <option value="old">🟠 Old</option>
    <option value="stale">🔴 Stale</option>
    <option value="archived">📦 Archived</option>
  </select>
  <span class="count" id="count"></span>
</div>
<table>
<thead class="bar-thead"><tr>
  <th class="num" data-key="num">#</th>
  <th data-key="name">Repository</th>
  <th class="r" data-key="date">Last commit</th>
  <th class="r age" data-key="days">Age</th>
  <th class="r" data-key="stars">Stars</th>
  <th data-key="label">Status</th>
</tr></thead>
<tbody id="body"></tbody>
</table>

{err_section}

<footer>Repo Freshness Checker v{VERSION} · buckets by age of last push:
🟢 &lt; 1 yr · 🟡 1-3 yrs · 🟠 3-5 yrs · 🔴 &gt; 5 yrs · 📦 archived</footer>
</div>
<script>window.__ROWS__ = {rows_json};</script>
<script>{_JS}</script>
</body>
</html>
"""
    Path(path).write_text(HTML, encoding="utf-8")


# ═══════════════════════════ GUI ════════════════════════════════
def run_gui():
    """Modern dark-themed GUI with tkinter."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    import webbrowser

    # ── colour palette ────────────────────────────────────────
    class Palette:
        BG        = "#1e1e2e"  # base
        SURFACE   = "#2a2a3c"  # surface0
        SURF_ALT  = "#313244"  # surface1
        TEXT      = "#cdd6f4"  # text
        SUBTEXT   = "#a6adc8"  # subtext0
        ACCENT    = "#89b4fa"  # blue
        GREEN     = "#a6e3a1"  # green
        RED       = "#f38ba8"  # red
        YELLOW    = "#f9e2af"  # yellow
        BORDER    = "#45475a"  # surface2
        HEADING   = "#b4befe"  # lavender

    # ── ttk theme builder ──────────────────────────────────────
    def _build_theme(style: ttk.Style) -> str:
        theme = "freshness"
        # Try to use an existing theme as base - "alt" works cross-platform
        available = style.theme_names()
        base = "alt" if "alt" in available else "default"
        style.theme_use(base)
        # We configure our custom colours on top of the base theme
        style.theme_create(theme, base, {
            "TFrame":       {"configure": {"background": Palette.BG}},
            "TLabel":       {"configure": {"background": Palette.BG,
                                           "foreground": Palette.TEXT}},
            "TButton":      {"configure": {"background": Palette.SURFACE,
                                           "foreground": Palette.TEXT,
                                           "borderwidth": 0,
                                           "padding": (16, 6)},
                             "map": {"background": [("active", Palette.ACCENT),
                                                    ("disabled", Palette.SURF_ALT)],
                                     "foreground": [("active", Palette.BG),
                                                    ("disabled", Palette.SUBTEXT)]}},
            "TCheckbutton": {"configure": {"background": Palette.BG,
                                           "foreground": Palette.TEXT}},
            "TEntry":       {"configure": {"fieldbackground": Palette.SURF_ALT,
                                           "foreground": Palette.TEXT,
                                           "insertcolor": Palette.TEXT}},
            "TSpinbox":     {"configure": {"fieldbackground": Palette.SURF_ALT,
                                           "foreground": Palette.TEXT,
                                           "buttonbackground": Palette.SURFACE}},
            "TLabelFrame":  {"configure": {"background": Palette.BG,
                                           "foreground": Palette.HEADING,
                                           "borderwidth": 1}},
            "TProgressbar": {"configure": {"background": Palette.ACCENT,
                                           "troughcolor": Palette.SURF_ALT,
                                           "borderwidth": 0}},
            "Horizontal.TProgressbar":
                            {"configure": {"background": Palette.ACCENT,
                                           "troughcolor": Palette.SURF_ALT,
                                           "borderwidth": 0}},
        })
        # Style for prominent action button
        style.configure("Accent.TButton", background=Palette.ACCENT,
                        foreground=Palette.BG, font=("sans-serif", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", "#74c7ec"), ("disabled", Palette.SURF_ALT)],
                  foreground=[("disabled", Palette.SUBTEXT)])
        style.configure("Stop.TButton", background=Palette.RED,
                        foreground=Palette.TEXT)
        style.map("Stop.TButton",
                  background=[("active", "#e64553"), ("disabled", Palette.SURF_ALT)])
        style.configure("Status.TLabel", background=Palette.SURFACE,
                        foreground=Palette.SUBTEXT, relief="sunken",
                        padding=(8, 2))
        style.configure("Heading.TLabel", font=("sans-serif", 11, "bold"),
                        foreground=Palette.HEADING)
        style.configure("Link.TLabel", foreground=Palette.ACCENT,
                        background=Palette.BG)
        style.theme_use(theme)
        return theme

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f"GitHub Repo Freshness Checker  v{VERSION}")
            self.geometry("880x680")
            self.minsize(720, 520)
            self.cancel_event = threading.Event()
            self.running = False
            self.input_files: list[str] = []
            # Token state: "value verified ok" + last result, so a check run
            # doesn't have to re-verify an unchanged token.
            self.tok_ok: str | None = None
            self.tok_info: dict | None = None
            self._verify_job: str | None = None
            self._live_ok = self._live_err = 0

            # Apply dark theme
            style = ttk.Style(self)
            _build_theme(style)

            # Configure root window
            self.configure(bg=Palette.BG)
            self.option_add("*foreground", Palette.TEXT)
            self.option_add("*background", Palette.BG)

            # Configure tk widgets that ttk doesn't cover
            self._tk_config()

            self._build()
            self._setup_keys()
            self._setup_drag_drop()
            saved = load_token()
            if saved:
                self.tok_var.set(saved)
                self.after(200, self._verify_token)
            self._load_config()

        def _tk_config(self):
            """Style plain tk widgets (not covered by ttk)."""
            self.option_add("*Entry.background", Palette.SURF_ALT)
            self.option_add("*Entry.foreground", Palette.TEXT)
            self.option_add("*Entry.insertBackground", Palette.TEXT)
            self.option_add("*Entry.highlightBackground", Palette.BORDER)
            self.option_add("*Entry.highlightColor", Palette.ACCENT)
            self.option_add("*Listbox.background", Palette.SURF_ALT)
            self.option_add("*Listbox.foreground", Palette.TEXT)
            self.option_add("*Text.background", Palette.SURF_ALT)
            self.option_add("*Text.foreground", Palette.TEXT)
            self.option_add("*Text.insertBackground", Palette.TEXT)

        # ── build UI ──────────────────────────────────────────
        def _build(self):
            pad = dict(padx=8, pady=5)
            main = ttk.Frame(self, padding=14)
            main.pack(fill="both", expand=True)

            # ── Header ────────────────────────────────────────
            header = ttk.Frame(main)
            header.pack(fill="x", **pad)
            ttk.Label(header, text="🔍 Repo Freshness Checker",
                      style="Heading.TLabel").pack(side="left")
            ttk.Label(header, text=f"v{VERSION}",
                      foreground=Palette.SUBTEXT).pack(side="left", padx=(6, 0))

            # ── Token section ─────────────────────────────────
            tf = ttk.LabelFrame(main, text="Authentication", padding=10)
            tf.pack(fill="x", **pad)

            # Token entry row
            row0 = ttk.Frame(tf)
            row0.pack(fill="x")
            ttk.Label(row0, text="Personal Access Token:").pack(side="left")
            self.tok_var = tk.StringVar()
            self.tok_entry = tk.Entry(row0, textvariable=self.tok_var,
                                      width=48, show="●",
                                      bg=Palette.SURF_ALT, fg=Palette.TEXT,
                                      insertbackground=Palette.TEXT,
                                      relief="flat", highlightthickness=1,
                                      highlightbackground=Palette.BORDER,
                                      highlightcolor=Palette.ACCENT)
            self.tok_entry.pack(side="left", fill="x", expand=True, padx=6)
            self._show_tok = False
            self.eye_btn = ttk.Button(row0, text="Show", width=5,
                                      command=self._toggle_tok)
            self.eye_btn.pack(side="left", padx=1)
            self.verify_btn = ttk.Button(row0, text="Verify", width=7,
                                         command=self._verify_token)
            self.verify_btn.pack(side="left", padx=1)
            ttk.Button(row0, text="Save", width=5,
                       command=self._save_tok).pack(side="left", padx=1)
            ttk.Button(row0, text="Clear", width=5,
                       command=self._clear_tok).pack(side="left", padx=1)
            # Re-check once typing settles, so the badge is never stale.
            self.tok_var.trace_add("write", self._on_tok_typed)

            # Token status + help link
            row1 = ttk.Frame(tf)
            row1.pack(fill="x", pady=(6, 0))
            self.tok_status = tk.Label(row1, text="Not verified",
                                       fg=Palette.SUBTEXT, bg=Palette.BG,
                                       anchor="w", font=("sans-serif", 9))
            self.tok_status.pack(side="left", fill="x", expand=True)
            link_lbl = tk.Label(row1,
                text="Generate a token ↗",
                fg=Palette.ACCENT, bg=Palette.BG,
                cursor="hand2", font=("sans-serif", 9))
            link_lbl.pack(side="right")
            link_lbl.bind("<Button-1>", lambda e: webbrowser.open(
                "https://github.com/settings/tokens"))
            link_lbl.bind("<Enter>", lambda e: link_lbl.configure(
                font=("sans-serif", 9, "underline")))
            link_lbl.bind("<Leave>", lambda e: link_lbl.configure(
                font=("sans-serif", 9)))

            # ── Files section ─────────────────────────────────
            ff = ttk.LabelFrame(main, text="Files", padding=10)
            ff.pack(fill="x", **pad)

            # Input
            fi0 = ttk.Frame(ff)
            fi0.pack(fill="x")
            ttk.Label(fi0, text="Input folder:").pack(side="left")
            self.in_var = tk.StringVar()
            self.in_entry = tk.Entry(fi0, textvariable=self.in_var,
                                     bg=Palette.SURF_ALT, fg=Palette.TEXT,
                                     insertbackground=Palette.TEXT,
                                     relief="flat", highlightthickness=1,
                                     highlightbackground=Palette.BORDER,
                                     highlightcolor=Palette.ACCENT)
            self.in_entry.pack(side="left", fill="x", expand=True, padx=6)
            ttk.Button(fi0, text="Choose folder…",
                       command=self._browse_in).pack(side="left")

            # Output
            fi1 = ttk.Frame(ff)
            fi1.pack(fill="x", pady=(8, 0))
            ttk.Label(fi1, text="Output file:").pack(side="left")
            self.out_var = tk.StringVar(value="freshness_report.md")
            self.out_entry = tk.Entry(fi1, textvariable=self.out_var,
                                      bg=Palette.SURF_ALT, fg=Palette.TEXT,
                                      insertbackground=Palette.TEXT,
                                      relief="flat", highlightthickness=1,
                                      highlightbackground=Palette.BORDER,
                                      highlightcolor=Palette.ACCENT)
            self.out_entry.pack(side="left", fill="x", expand=True, padx=6)
            ttk.Button(fi1, text="Browse…",
                       command=self._browse_out).pack(side="left")

            fi2 = ttk.Frame(ff)
            fi2.pack(fill="x", pady=(8, 0))
            ttk.Label(fi2, text="Format:").pack(side="left")
            self.fmt_var = tk.StringVar(value="both")
            ttk.Radiobutton(fi2, text="Markdown", value="md",
                            variable=self.fmt_var).pack(side="left")
            ttk.Radiobutton(fi2, text="HTML", value="html",
                            variable=self.fmt_var).pack(side="left", padx=6)
            ttk.Radiobutton(fi2, text="Both", value="both",
                            variable=self.fmt_var).pack(side="left")
            ttk.Label(fi2, text="   HTML gets an interactive filter and sort",
                      foreground=Palette.SUBTEXT).pack(side="left")

            # ── Controls ──────────────────────────────────────
            cf = ttk.Frame(main)
            cf.pack(fill="x", **pad)

            self.start_btn = ttk.Button(
                cf, text="▶  Start Checking", style="Accent.TButton",
                command=self._start)
            self.start_btn.pack(side="left")

            self.stop_btn = ttk.Button(
                cf, text="⬛  Stop", style="Stop.TButton",
                command=self._stop, state="disabled")
            self.stop_btn.pack(side="left", padx=6)

            self.open_btn = ttk.Button(
                cf, text="📂 Open Report", command=self._open_report,
                state="disabled")
            self.open_btn.pack(side="left")

            ttk.Label(cf, text="Workers:").pack(side="left", padx=(24, 2))
            self.workers_var = tk.IntVar(value=DEFAULT_WORKERS)
            self.workers_spin = ttk.Spinbox(
                cf, from_=1, to=50, width=4,
                textvariable=self.workers_var)
            self.workers_spin.pack(side="left")

            # ── Progress ──────────────────────────────────────
            pf = ttk.Frame(main)
            pf.pack(fill="x", **pad)
            self.prog = ttk.Progressbar(pf, mode="determinate")
            self.prog.pack(side="left", fill="x", expand=True)
            self.prog_lbl = ttk.Label(pf, text="  0 / 0", width=26)
            self.prog_lbl.pack(side="left", padx=6)

            # ── Log ───────────────────────────────────────────
            lf = ttk.LabelFrame(main, text="Log", padding=6)
            lf.pack(fill="both", expand=True, **pad)

            if sys.platform == "win32":
                log_font = ("Consolas", 10)
            elif sys.platform == "darwin":
                log_font = ("Menlo", 11)
            else:
                log_font = ("monospace", 10)

            self.log = scrolledtext.ScrolledText(
                lf, height=12, state="disabled", wrap="word",
                font=log_font,
                bg=Palette.SURF_ALT, fg=Palette.TEXT,
                insertbackground=Palette.TEXT,
                relief="flat", borderwidth=0,
                padx=6, pady=4)
            self.log.pack(fill="both", expand=True)

            # Colour tags for log
            self.log.tag_config("ok", foreground=Palette.GREEN)
            self.log.tag_config("err", foreground=Palette.RED)
            self.log.tag_config("info", foreground=Palette.SUBTEXT)
            self.log.tag_config("warn", foreground=Palette.YELLOW)
            self.log.tag_config("accent", foreground=Palette.ACCENT)
            self.log.tag_config("bold", font=(log_font[0], log_font[1], "bold"))

            # ── Status bar ────────────────────────────────────
            self.status_var = tk.StringVar(value="Ready")
            status_bar = ttk.Label(main, textvariable=self.status_var,
                                   style="Status.TLabel")
            status_bar.pack(fill="x", **pad)

        # ── drag & drop ──────────────────────────────────────
        def _setup_drag_drop(self):
            try:
                self.in_entry.drop_target_register("*")
                self.in_entry.dnd_bind("<<Drop>>", self._on_drop)
                self.in_entry.master.drop_target_register("*")
                self.in_entry.master.dnd_bind("<<Drop>>", self._on_drop)
            except (AttributeError, tk.TclError):
                self.in_entry.bind("<Button-3>", self._paste_context_menu)
                self.in_entry.bind("<<Paste>>", self._on_paste)

        def _on_drop(self, event):
            raw = getattr(event, "data", "")
            if not raw:
                return
            path = raw.strip("{}").strip()
            if path.startswith("file://"):
                path = path[7:]
            if path.startswith("file:"):
                path = path[5:]
            from urllib.parse import unquote
            path = unquote(path)
            if Path(path).is_dir():
                self.input_files = find_markdown_files(path)
                self._update_input_display()
            elif Path(path).is_file():
                self.input_files = [str(Path(path).resolve())]
                self._update_input_display()

        def _on_paste(self, event=None):
            self.after(10, self._check_clipboard_for_path)

        def _paste_context_menu(self, event):
            menu = tk.Menu(self, tearoff=0, bg=Palette.SURFACE,
                           fg=Palette.TEXT, activebackground=Palette.ACCENT,
                           activeforeground=Palette.BG)
            menu.add_command(label="Paste path",
                             command=self._check_clipboard_for_path)
            menu.add_command(label="Browse…", command=self._browse_in)
            menu.tk_popup(event.x_root, event.y_root)

        def _check_clipboard_for_path(self):
            try:
                raw = self.clipboard_get().strip()
                path = raw.strip("'\"").strip()
                if path.startswith("file://"):
                    path = path[7:]
                if Path(path).is_dir():
                    self.input_files = find_markdown_files(path)
                    self._update_input_display()
                elif Path(path).is_file():
                    self.input_files = [str(Path(path).resolve())]
                    self._update_input_display()
            except (tk.TclError, OSError):
                pass

        # ── keyboard shortcuts ───────────────────────────────
        def _setup_keys(self):
            self.bind("<Control-o>", lambda e: self._browse_in())
            self.bind("<Control-s>", lambda e: self._save_tok())
            self.bind("<Control-Return>", lambda e: self._start())
            self.bind("<Escape>", lambda e: self._stop())
            # Enter in the output field is the common "I'm done, run it".
            self.out_entry.bind("<Return>", lambda e: self._start())
            self.tok_entry.bind("<Return>", lambda e: self._verify_token())

        # ── config persistence ────────────────────────────────
        CONFIG_FILE = CONFIG_DIR / "gui_config.json"

        def _load_config(self):
            try:
                if self.CONFIG_FILE.exists():
                    data = json.loads(self.CONFIG_FILE.read_text())
                    if "last_input" in data:
                        saved = data["last_input"]
                        if isinstance(saved, str):
                            saved = [saved]
                        self.input_files = []
                        for p in saved:
                            if Path(p).is_dir():
                                self.input_files.extend(find_markdown_files(p))
                            elif Path(p).is_file():
                                self.input_files.append(str(Path(p)))
                        self._update_input_display()
                    if "last_output" in data:
                        self.out_var.set(data["last_output"])
                    if "last_workers" in data:
                        self.workers_var.set(data["last_workers"])
                    if "last_format" in data:
                        self.fmt_var.set(data["last_format"])
            except (json.JSONDecodeError, OSError):
                pass

        def _save_config(self):
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                data = {
                    "last_input": self.input_files,
                    "last_output": self.out_var.get(),
                    "last_workers": self.workers_var.get(),
                }
                self.CONFIG_FILE.write_text(
                    json.dumps(data, indent=2), encoding="utf-8")
            except OSError:
                pass

        # ── token ──────────────────────────────────────────────
        def _toggle_tok(self):
            self._show_tok = not self._show_tok
            self.tok_entry.config(show="" if self._show_tok else "●")
            self.eye_btn.config(text="Hide" if self._show_tok else "Show")

        def _on_tok_typed(self, *_):
            """Debounce: check the token shortly after typing stops."""
            typed = self.tok_var.get().strip()
            if typed == (self.tok_ok or ""):
                return
            self.tok_ok = None
            self._set_tok_status("Not verified", Palette.SUBTEXT)
            if self._verify_job:
                self.after_cancel(self._verify_job)
            if not typed:
                self._set_tok_status(
                    "No token - unauthenticated (60 requests/hour)",
                    Palette.YELLOW)
                return
            self._verify_job = self.after(1200, self._verify_token)

        def _set_tok_status(self, text: str, color: str):
            self.tok_status.config(text=text, fg=color)

        def _verify_token(self, event=None):
            """Ask GitHub whether the token actually works (off-thread)."""
            token = self.tok_var.get().strip()
            self._verify_job = None
            if not token:
                self._set_tok_status(
                    "No token - unauthenticated (60 requests/hour)",
                    Palette.YELLOW)
                return None
            self.verify_btn.config(state="disabled", text="Checking")
            self._set_tok_status("Checking token with GitHub …", Palette.SUBTEXT)
            threading.Thread(target=self._verify_worker, args=(token,),
                             daemon=True).start()
            return None

        def _verify_worker(self, token: str):
            info = check_token(token)
            self.after(0, lambda: self._apply_token_info(info, token))

        def _apply_token_info(self, info: dict, token: str | None = None):
            self.verify_btn.config(state="normal", text="Verify")
            # Ignore a result that arrived after the field changed again.
            if token is not None and token != self.tok_var.get().strip():
                return
            self.tok_info = info
            self.tok_ok = token if info["ok"] else None
            color = {"ok": Palette.GREEN, "warn": Palette.YELLOW,
                     "error": Palette.RED}[info["level"]]
            prefix = {"ok": "✓", "warn": "⚠", "error": "✗"}[info["level"]]
            msg = f"{prefix} {info['message']}"
            if info["ok"] and info["limit"]:
                msg += f" · {info['limit']:,} requests/hour"
            self._set_tok_status(msg, color)

        def _save_tok(self):
            t = self.tok_var.get().strip()
            if not t:
                self._set_tok_status("Nothing to save", Palette.YELLOW)
                return
            # Only accept a token GitHub has confirmed, so a typo can't be
            # persisted and silently break the next run.
            if self.tok_ok != t:
                self._verify_token()
                messagebox.showinfo(
                    "Verifying token",
                    "Checking the token with GitHub first — save it once "
                    "it comes back valid.")
                return
            save_token(t)
            self._set_tok_status(f"✓ Saved to {TOKEN_FILE}", Palette.GREEN)

        def _clear_tok(self):
            self.tok_var.set("")
            clear_saved_token()
            self._set_tok_status("Token removed", Palette.SUBTEXT)

        def _browse_in(self):
            folder = filedialog.askdirectory(title="Select folder containing Markdown files")
            if folder:
                self.input_files = find_markdown_files(folder)
                if not self.input_files:
                    messagebox.showwarning(
                        "No Markdown files",
                        "The selected folder contains no Markdown files.",
                    )
                self._update_input_display()

        def _update_input_display(self):
            if len(self.input_files) == 1:
                self.in_var.set(self.input_files[0])
            elif self.input_files:
                self.in_var.set(f"{len(self.input_files)} Markdown files found")
            else:
                self.in_var.set("")

        def _browse_out(self):
            fmt = self.fmt_var.get()
            if fmt == "html":
                types, ext = [("HTML", "*.html")], ".html"
            elif fmt == "both":
                types, ext = [("Markdown", "*.md"), ("HTML", "*.html")], ".md"
            else:
                types, ext = [("Markdown", "*.md")], ".md"
            p = filedialog.asksaveasfilename(
                title="Save report as",
                defaultextension=ext,
                filetypes=types)
            if p:
                self.out_var.set(p)

        def _report_paths(self, fmt: str | None = None) -> list[str]:
            """Paths to write for the chosen format, in display order."""
            fmt = fmt or self.fmt_var.get()
            out = self.out_var.get().strip()
            base = Path(out).with_suffix("")
            if fmt == "html":
                return [str(base) + ".html"]
            if fmt == "both":
                return [str(base) + ".md", str(base) + ".html"]
            return [out]

        def _open_report(self):
            fmt = self.fmt_var.get()
            paths = self._report_paths(fmt)
            existing = [p for p in paths if Path(p).exists()]
            if not existing:
                messagebox.showerror(
                    "No report",
                    f"Report not found at:\n{paths[0]}\n\n"
                    "Run a check first.")
                return
            # Prefer the HTML report - it is the interactive one.
            target = existing[-1]
            import subprocess, platform
            s = platform.system()
            if s == "Darwin":
                subprocess.Popen(["open", target])
            elif s == "Windows":
                os.startfile(target)
            else:
                subprocess.Popen(["xdg-open", target])

        def _log(self, msg: str):
            """Log with automatic tag detection based on emoji prefixes."""
            msg = self._sanitize(msg)
            self.log.config(state="normal")
            # Determine tag from message prefix
            tag = None
            if msg.startswith("✅") or msg.startswith("📝"):
                tag = "ok"
            elif msg.startswith("❌") or msg.startswith("💥"):
                tag = "err"
            elif msg.startswith("⏳") or msg.startswith("⚠"):
                tag = "warn"
            elif msg.startswith("🔑") or msg.startswith("⚙"):
                tag = "accent"
            elif msg.startswith("["):
                # Line like "[1/523] ✅ …" - keep the running tallies.
                if "✅" in msg:
                    self._live_ok += 1
                elif "❌" in msg:
                    self._live_err += 1
                tag = "info"
            if tag:
                self.log.insert("end", msg + "\n", tag)
            else:
                self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.config(state="disabled")

        def _progress(self, cur, total, dyn_info=None):
            pct = cur / total * 100 if total else 0
            self.prog["value"] = pct
            ok, err = getattr(self, "_live_ok", 0), getattr(self, "_live_err", 0)
            label = f"  {cur} / {total}"
            if dyn_info and dyn_info.get("eta", 0) > 0:
                eta = dyn_info["eta"]
                if eta >= 3600:
                    label += f"  ·  ETA {eta/3600:.0f}h"
                elif eta >= 60:
                    label += f"  ·  ETA {eta/60:.0f}m"
                else:
                    label += f"  ·  ETA {eta:.0f}s"
            if ok or err:
                label += f"  ·  ✓{ok} ✗{err}"
            self.prog_lbl.config(text=label)

        # ── threaded work ─────────────────────────────────────
        def _start(self):
            if self.running:
                return
            inp = list(self.input_files)
            if not inp:
                typed_input = self.in_var.get().strip()
                if typed_input:
                    inp = [typed_input]
            out = self.out_var.get().strip()
            tok = self.tok_var.get().strip()
            if not inp or any(not Path(path).is_file() for path in inp):
                messagebox.showerror(
                    "No input",
                    "Pick a folder containing Markdown files (or type a "
                    "file path) first.")
                self.in_entry.focus_set()
                return
            if not out:
                messagebox.showerror("No output", "Specify an output file.")
                self.out_entry.focus_set()
                return

            self.running = True
            self.cancel_event.clear()
            self._live_ok = self._live_err = 0
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.open_btn.config(state="disabled")
            self.log.config(state="normal")
            self.log.delete("1.0", "end")
            self.log.config(state="disabled")
            self.prog["value"] = 0
            self.prog_lbl.config(text="  0 / 0")
            self.status_var.set("Checking token …")

            # Resolve output paths here: the worker thread must not touch
            # Tk variables.
            threading.Thread(
                target=self._worker, args=(inp, tok, self._report_paths()),
                daemon=True).start()

        def _stop(self):
            if not self.running:
                return
            self.cancel_event.set()
            self.stop_btn.config(state="disabled")
            self.status_var.set("Cancelling …")

        def _worker(self, inp, tok, out_paths):
            try:
                # ── token preflight ──────────────────────────
                # Fail fast and loudly rather than reporting every repo as
                # "Forbidden" once an invalid token is used.
                self._safe_log("🔑 Checking token …")
                info = check_token(tok)
                self.after(0, lambda i=info: self._apply_token_info(i))
                if not info["ok"] and tok:
                    self._safe_log(f"❌ {info['message']}")
                    self._safe_status("Token rejected by GitHub")
                    self.after(0, lambda i=info: messagebox.showerror(
                        "Invalid GitHub token", i["message"]))
                    return
                if tok and info["limit"]:
                    self._safe_log(
                        f"✅ {info['message']} · "
                        f"{info['limit']:,} requests/hour")
                elif tok:
                    self._safe_log(f"✅ {info['message']}")
                else:
                    self._safe_log(
                        f"⚠  {info['message']} — this will be slow for "
                        f"long lists")

                # ── input ────────────────────────────────────
                self._safe_log(f"📂 Reading {len(inp)} input file(s) …")
                repos = extract_repos(expand_input_paths(inp))
                self._safe_log(f"🔍 Found {len(repos)} unique GitHub repos")
                if not repos:
                    self._safe_status("No GitHub URLs found in the input.")
                    self.after(0, lambda: messagebox.showwarning(
                        "Nothing to check",
                        "No github.com links were found in the selected "
                        "files."))
                    return

                workers = self.workers_var.get()
                self._safe_log(f"⚙  {workers} concurrent workers")
                self._safe_status(f"Checking {len(repos)} repositories …")
                results, errors = process_repos(
                    repos, tok,
                    progress_cb=self._safe_progress,
                    log_cb=self._safe_log,
                    cancel=self.cancel_event,
                    max_workers=workers,
                )

                if not results and not errors:
                    self._safe_status("Nothing to report.")
                    return

                for path in out_paths:
                    if path.endswith(".html"):
                        generate_html_report(results, errors, path)
                    else:
                        generate_report(results, errors, path)
                    self._safe_log(f"📝 Saved → {path}")
                self._safe_log(
                    f"   ✅ {len(results)} reachable  |  "
                    f"❌ {len(errors)} unreachable")
                self._safe_status(
                    f"Done — {len(results)} of {len(results) + len(errors)} "
                    f"repos reported")
                self.after(0, lambda: self.open_btn.config(state="normal"))
            except Exception as exc:
                self._safe_log(f"\n💥 ERROR: {exc}")
                self._safe_status(f"Error: {exc}")
            finally:
                self._finish()
                self.after(0, self._save_config)

        def _finish(self):
            self.running = False
            self.after(0, lambda: self.start_btn.config(state="normal"))
            self.after(0, lambda: self.stop_btn.config(state="disabled"))

        def _sanitize(self, text: str) -> str:
            """Redact token from any displayed text to prevent accidental leaks."""
            token = self.tok_var.get().strip()
            if token and token in text:
                text = text.replace(token, "***")
            return text

        # thread-safe helpers
        def _safe_log(self, msg):
            self.after(0, self._log, msg)

        def _safe_progress(self, cur, total, dyn_info=None):
            self.after(0, self._progress, cur, total, dyn_info)

        def _safe_status(self, msg):
            self.after(0, lambda m=msg: self.status_var.set(self._sanitize(m)))

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

    # ── token preflight ────────────────────────────────────────
    # A bad token makes every repo fail with a bare "Forbidden", so verify
    # it up front and say exactly what is wrong.
    print("🔑  Checking token …")
    info = check_token(token)
    if not info["ok"] and token:
        print(f"\n✗  {info['message']}", file=sys.stderr)
        if args.token:
            print(f"   Supplied via --token (length {len(args.token)}).",
                  file=sys.stderr)
        else:
            print(f"   Read from {TOKEN_FILE} or $GITHUB_TOKEN.",
                  file=sys.stderr)
        print("   Run without a token to check public repos unauthenticated "
              "(60 requests/hour).", file=sys.stderr)
        sys.exit(2)
    if args.save_token and args.token:
        save_token(args.token)
        print(f"✓  Token saved to {TOKEN_FILE}")
    if info["ok"]:
        print(f"✓  {info['message']}"
              + (f"  ·  {info['limit']:,} requests/hour"
                 if info["limit"] else ""))
    else:
        print(f"⚠  {info['message']}")

    expanded = expand_input_paths(args.input_file)
    repos = extract_repos(expanded)
    print(f"🔍  Found {len(repos)} unique GitHub repos in "
          f"{len(expanded)} input file(s)")
    if not repos:
        sys.exit(0)

    print(f"⚙  Using {args.workers} concurrent workers")
    start = time.time()

    # Progress owns the current terminal line; log lines have to wipe it
    # first, otherwise the two overwrite each other into noise.
    class _Progress:
        width = 0
        bucket = -1

        def __call__(self, cur, total, dyn_info=None):
            pct = cur / total * 100 if total else 0
            label = f"  {cur}/{total} ({pct:.0f}%)"
            if dyn_info and dyn_info.get("eta", 0) > 0:
                eta = dyn_info["eta"]
                label += (f"  ETA {eta/3600:.0f}h" if eta >= 3600
                          else f"  ETA {eta/60:.0f}m" if eta >= 60
                          else f"  ETA {eta:.0f}s")
            if sys.stdout.isatty():
                self.clear()
                sys.stdout.write(label)
                sys.stdout.flush()
                self.width = len(label)
            else:
                # Piped output: no carriage returns, one line per 10%.
                step = int(pct // 10)
                if step != self.bucket:
                    self.bucket = step
                    print(label)

        def clear(self):
            if self.width:
                sys.stdout.write("\r" + " " * self.width + "\r")
                sys.stdout.flush()
                self.width = 0

    progress = _Progress()

    def _cli_log(msg: str):
        progress.clear()
        print(msg)

    results, errors = process_repos(
        repos, token,
        progress_cb=progress,
        log_cb=_cli_log,
        max_workers=args.workers,
    )
    elapsed = time.time() - start
    progress.clear()

    generate_report(results, errors, args.output)
    print(f"📝  Report saved → {args.output}")

    if args.format in ("html", "both"):
        html_path = str(Path(args.output).with_suffix('.html'))
        generate_html_report(results, errors, html_path)
        print(f"🌐  HTML report  → {html_path}")

    print(f"    ✅ {len(results)} reachable  |  ❌ {len(errors)} unreachable"
          f"  |  ⏱ {elapsed:.1f}s")
    sys.exit(1 if errors and not results else 0)

# ════════════════════════ entry point ═══════════════════════════
if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--gui"):
        run_cli()
    else:
        run_gui()