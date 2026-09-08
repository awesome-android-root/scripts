# repo_freshness_checker

Checks GitHub repositories listed in a file (such as a category page under `docs/apps-and-modules/`) for their
last-update dates via the GitHub API. Produces a sorted Markdown or HTML report with health scores, star counts,
and freshness metrics. Supports parallel API requests for speed.

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
