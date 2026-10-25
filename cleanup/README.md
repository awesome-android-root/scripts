# cf-pages-cleanup

Deletes old Cloudflare Pages deployments for a project, keeping only the N most recent.

## Requirements

- [Bun](https://bun.sh)
- A Cloudflare API token with **Pages: Edit** permission
- Your Cloudflare Account ID

## Setup (fish shell)

```fish
set -x CF_API_TOKEN your_token_here
set -x CF_ACCOUNT_ID your_account_id_here
```

To persist across sessions, add these to `~/.config/fish/config.fish`.

Get your API token: Cloudflare dashboard → My Profile → API Tokens → Create Token.
Get your Account ID: Cloudflare dashboard → right sidebar on any domain/account page.

## Usage

```fish
# Preview
bun run cf-pages-cleanup.ts -p awesome-android-root --dry-run

# Delete, with 5 parallel jobs
bun run cf-pages-cleanup.ts -p awesome-android-root -j 5
```

Flags:

- `-p, --project <name>` - Pages project name (default `awesome-android-root`)
- `-j, --jobs <count>` - number of deletions to run concurrently (default `5`)
- `--dry-run` - list what would be deleted without deleting

The script keeps the 5 most recent deployments (plus the live production deployment). To change that, edit `KEEP` near the top of the script:

```ts
const KEEP = 5;
```

## Notes

- Deletion uses `force=true`, so it also removes preview deployments with active aliases (e.g. branch URLs pointing at them).
- The live production deployment is never deleted, even if it falls outside the newest N deployments.
- The script paginates through all deployments before deciding what to delete, so it works regardless of how many exist.

## Troubleshooting

- `Missing CF_API_TOKEN or CF_ACCOUNT_ID env vars` - the env vars aren't set in your current shell session.
- `403` errors - token lacks Pages:Edit permission or wrong account ID.
- `404` on list - check `PROJECT` name matches exactly what's in the Cloudflare dashboard.
