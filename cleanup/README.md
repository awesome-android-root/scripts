# cf-pc

Deletes old Cloudflare Pages deployments for a project, keeping only the N most recent.

## Install

```bash
npm i -g cf-pc
```

or, with Bun:

```bash
bun add -g cf-pc
```

Requires Node 26+ or Bun. The published `cf-pc` command is compiled JavaScript, so installing needs no build tools or Bun; Bun is only needed to build from source.

## Credentials

`cf-pc` needs two environment variables:

- `CF_API_TOKEN` - a Cloudflare API token with **Pages: Edit** permission. Get it from the Cloudflare dashboard → My Profile → API Tokens → Create Token.
- `CF_ACCOUNT_ID` - your Cloudflare Account ID. Get it from the Cloudflare dashboard → right sidebar on any domain/account page.

Set them in fish:

```fish
set -x CF_API_TOKEN your_token_here
set -x CF_ACCOUNT_ID your_account_id_here
```

To persist across sessions, add these to `~/.config/fish/config.fish`.

## Usage

```bash
# Preview what would be deleted
cf-pc -p awesome-android-root --dry-run

# Delete, keeping the 10 newest, with 5 parallel jobs
cf-pc -p awesome-android-root -k 10 -j 5

# Print the installed version
cf-pc --version

# Show help
cf-pc --help
```

The project name is always required.

## Flags

| Flag | Description |
|------|-------------|
| `-p, --project <name>` | Pages project name (required) |
| `-k, --keep <count>` | deployments to keep (default `5`; `0` deletes all but the live production deployment) |
| `-j, --jobs <count>` | concurrent deletions (default `5`) |
| `--dry-run` | list what would be deleted without deleting |
| `--version` | print the version |
| `-h, --help` | show help |

## Behavior notes

- The newest N deployments are kept, plus the live production deployment, which is never deleted even if it falls outside the newest N.
- Deletion uses `force=true`, so it also removes preview deployments with active aliases (e.g. branch URLs pointing at them).
- The script paginates through all deployments before deciding what to delete, so it works regardless of how many exist.
- Output is colored when attached to a terminal; piped output (CI, logs) is plain. Set `NO_COLOR` to disable colors explicitly.

## Troubleshooting

- `Missing CF_API_TOKEN or CF_ACCOUNT_ID env vars.` - the env vars aren't set in your current shell session.
- `401` errors - the API token is invalid or expired.
- `403` errors - the token lacks **Pages: Edit** permission or the Account ID is wrong.
- `404` on list - check the `-p` project name matches exactly what's in the Cloudflare dashboard.

## Publishing (maintainer)

```bash
npm pack --dry-run   # confirm the tarball contents
npm publish          # needs npm login (2FA)
```

For future releases, bump `version` in `package.json` and run `npm publish` again.
