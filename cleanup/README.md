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
bun run cf-pages-cleanup.ts
```

By default this targets the `awesome-android-root` project and keeps the 5 most recent deployments. To change either, edit these lines near the top of the script:

```ts
const PROJECT = "awesome-android-root";
const KEEP = 5;
```

## Notes

- Deletion uses `force=true`, so it also removes deployments with active aliases (e.g. production/preview URLs pointing at them). Remove `force=true` in `deleteDeployment` if you'd rather have it skip those.
- The script paginates through all deployments before deciding what to delete, so it works regardless of how many exist.
- No dry-run mode currently. Test on a low-stakes project first if unsure.

## Troubleshooting

- `Missing CF_API_TOKEN or CF_ACCOUNT_ID env vars` - the env vars aren't set in your current shell session.
- `403` errors - token lacks Pages:Edit permission or wrong account ID.
- `404` on list - check `PROJECT` name matches exactly what's in the Cloudflare dashboard.
