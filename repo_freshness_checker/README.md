# repo_freshness_checker

Checks GitHub repositories listed in a file (such as a category page under `docs/apps-and-modules/`) for their
last-push dates via the GitHub API. Produces a sorted Markdown or HTML report with health scores, star counts,
and freshness metrics. Supports parallel API requests for speed.

Supports both a graphical interface and command-line mode.

### Requirements

```bash
pip install -r scripts/repo_freshness_checker/requirements.txt
```

The GUI additionally needs Tk (`sudo pacman -S tk` on Arch, `sudo apt install python3-tk` on Debian/Ubuntu,
`brew install python-tk` on macOS). The CLI works without it.

### Token

Any GitHub token works; public repositories need no scopes at all. Unauthenticated requests are capped at
60/hour, so a token is worth having for anything longer than a handful of repos.

The token is checked against the API (`GET /user`) before checking starts, so an invalid or revoked token is
reported immediately instead of failing once per repository.

### CLI Usage

```bash
python repo_freshness_checker/repo_freshness_checker.py docs/apps-and-modules/privacy.md -o report.md -t <github_token>
# Check several category pages; duplicate repositories are checked only once
python repo_freshness_checker/repo_freshness_checker.py docs/apps-and-modules/*.md -o report.md -t <github_token>
# Interactive HTML report alongside (or instead of) the Markdown one
python repo_freshness_checker/repo_freshness_checker.py docs/ -o report.md -f both -t <github_token>
```

Exits with status 2 if the token is rejected, 1 if every repository failed to load.

### GUI Usage

```bash
python repo_freshness_checker/repo_freshness_checker.py
```

The GUI input picker lets you choose a folder and automatically reads all `.md` files in it and its subfolders.
Repositories are de-duplicated across all discovered files.

The token field shows its verification state as you type (✓ valid, ✗ rejected, ⚠ unauthenticated) and a token is
only saved to disk once GitHub has confirmed it. Shortcuts: `Ctrl+O` choose folder, `Ctrl+S` save token,
`Enter` in the output field starts a run, `Esc` cancels it.

### HTML report

Standalone, no dependencies, and works from `file://`. Sortable columns, live text filter, status filter,
and a light/dark toggle that remembers your choice. Reachability failures are grouped by cause.
