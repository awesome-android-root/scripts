# cf-pc: publish as a global npm CLI

Date: 2026-11-18
Status: proposed

## Intent

Make the Cloudflare Pages cleanup script installable once and runnable from any
folder as `cf-pc`, published under the npm user `fynks`. Personal tool, not a
product: no discovery/marketing work, no compatibility beyond the author's
machines.

## Goals

- `npm i -g cf-pc` (or `bun add -g cf-pc`) yields a `cf-pc` command on PATH.
- `cf-pc -p <project> --dry-run` works from any directory.
- Nothing to build: the published artifact is the source file.
- `-k/--keep` replaces the hardcoded `KEEP = 5`.
- Project name is required; no built-in default.

## Non-goals

- Publishing to other registries, CI automation, GitHub Actions, changelogs.
- Windows support, older Node versions, a JS build artifact.
- Tests beyond argument-parsing and a real dry run against the live API.

## Constraints

- Node 26 strips TypeScript types natively, so a `.ts` file can be a bin entry.
  Verified locally: `node cf-pages-cleanup.ts --help` runs on Node 26.13.1.
- Only erasable TypeScript syntax may be used (no enums, namespaces,
  parameter properties). The current file already complies.
- Package name `cf-pc` is unclaimed on npm.
- The git remote is `awesome-android-root/scripts`, with this code in `cleanup/`.

## Approaches considered

- **A. Publish the `.ts` file as-is, shebang `node`.** No build step, published
  file equals edited file. Chosen.
- **B. Same file, shebang `bun`.** Simpler on Bun-only machines, but the
  command only exists where Bun is installed.
- **C. Build `dist/cli.js` with `bun build`.** Standard artifact, but adds a
  build step and a stale-`dist` failure mode for no benefit here.

## Design

### Files

```
cleanup/
  package.json          new: npm metadata and bin wiring
  cf-pages-cleanup.ts   edited: shebang, required project, -k, --version
  README.md             edited: install and global usage
  test/cli.test.ts      new: argument-parsing tests via node:test
```

`test/` is development-only and excluded from the tarball by the `files`
whitelist.

`package.json`:

- `name: "cf-pc"`, `version: "0.1.0"`, `description`, `license: "MIT"`.
- `type: "module"`.
- `bin: { "cf-pc": "./cf-pages-cleanup.ts" }`.
- `files: ["cf-pages-cleanup.ts", "README.md"]` so the tarball is only those
  three files including package.json.
- `engines: { "node": ">=26" }`.
- `repository` pointing at `awesome-android-root/scripts`, `directory: "cleanup"`.
- `keywords`: cloudflare, pages, deployments, cleanup, cli.
- `prepublishOnly`: run the CLI with `--help` as a smoke check that the file
  parses and executes.

### CLI changes in `cf-pages-cleanup.ts`

- Shebang becomes `#!/usr/bin/env node`.
- Remove `DEFAULT_PROJECT`; `-p/--project` is required. Missing/blank project
  prints the usage plus a clear message and exits 1. `--help` still wins and
  exits 0.
- Add `-k/--keep <count>`, default `5`, integer `>= 0`. Validation mirrors the
  existing jobs validation.
- Add `--version`, reading the version from `./package.json` via a JSON import
  with `with { type: "json" }`, so it cannot drift.
- Usage text refers to `cf-pc`, not `bun cf-pages-cleanup.ts`.
- Keep all current behavior: pagination, sorting, keeping the newest N plus the
  live production deployment, concurrent deletes with `force=true`, spinner,
  color handling, failure summary and exit code.

### README

- Install: `npm i -g cf-pc` or `bun add -g cf-pc`.
- Usage from any folder: `cf-pc -p <project> --dry-run`,
  `cf-pc -p <project> -k 10 -j 5`.
- Flags table gains `-k/--keep` and `--version`; `-p` marked required.
- Note the Node 26+ requirement and the required env vars.

### Publishing (manual, by the author)

1. `npm login` (2FA as prompted).
2. `npm pack --dry-run` to confirm the three-file tarball.
3. `npm publish` from `cleanup/`.
4. Verify with `npm i -g cf-pc` and `cf-pc --version` from an unrelated folder.
5. Future releases: bump `version`, publish.

## Verification

- `node cf-pages-cleanup.ts` with no args: usage error, exit 1.
- `--help`, `--version`, `-k` validation (non-integer, negative).
- `npm pack --dry-run`: expected file list and size.
- Global install of the packed tarball; run `cf-pc --version` and `cf-pc` from
  `/tmp`; then remove the temporary global install.
- One real `--dry-run` against the author's account and a real delete on a
  scratch project, if the author chooses.

## Left to the builder

- Whether `--version` output is just the number or `cf-pc 0.1.0`.
- Wording of error messages.
- Whether to keep the filename `cf-pages-cleanup.ts` or rename to `cf-pc.ts`
  (default: keep).
