# repo_freshness_checker

Checks GitHub repositories listed in a file (such as a category page under `docs/apps-and-modules/`) for their
last-update dates via the GitHub API. Produces a sorted Markdown or HTML report with health scores, star counts,
and freshness metrics.

When a token is supplied it uses **GraphQL batch queries** (up to 100 repos per request, so a 500-repo list needs
only ~5 requests and a fraction of the rate-limit budget); it automatically falls back to parallel REST API
requests when GraphQL is unavailable or a batch keeps failing (rate limits, timeouts), so results are identical
either way. Secondary rate limits (`Retry-After`) are honoured instead of being reported as errors.

The generated **HTML report** is a single self-contained file (no external assets) with light/dark themes, a
summary card row, clickable status filter chips with counts, instant search, sortable columns (oldest first by
default), CSV export of the visible rows, and a collapsible errors section. Keyboard: `/` focuses search,
`Esc` clears it.

The **GUI** is a flat, native-toolkit interface (no webview): source/report fields with live hints, token field
with show/hide and optional on-device remembering, drag & drop or clipboard paste of folders/files, per-run
worker control, an activity log with colour-coded lines, and an inline progress/ETA status bar.

Supports both a graphical interface and command-line mode.

### Requirements

```bash
pip install -r scripts/repo_freshness_checker/requirements.txt
```

### CLI Usage

```bash
python repo_freshness_checker/repo_freshness_checker.py docs/apps-and-modules/privacy.md -o report.md -t <github_token>
# Check several category pages; duplicate repositories are checked only once
python repo_freshness_checker/repo_freshness_checker.py docs/apps-and-modules/*.md -o report.md -t <github_token>
```

### GUI Usage

```bash
python repo_freshness_checker/repo_freshness_checker.py
```

The GUI input picker lets you choose a folder and automatically reads all `.md` files in it and its subfolders. Repositories are de-duplicated across all discovered files.
