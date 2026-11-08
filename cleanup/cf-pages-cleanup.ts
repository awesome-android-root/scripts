#!/usr/bin/env bun
// Deletes all Cloudflare Pages deployments for a project except the N most recent.
// Requires: CF_API_TOKEN, CF_ACCOUNT_ID env vars.
// Usage: bun cf-pages-cleanup.ts [-p project] [-j jobs] [--dry-run]

import { parseArgs } from "node:util";

const KEEP = 5;
const DEFAULT_PROJECT = "awesome-android-root";
const DEFAULT_JOBS = 5;
const USAGE = "Usage: bun cf-pages-cleanup.ts [-p project] [-j jobs] [-h] [--dry-run]";

const COLORS = process.stdout.isTTY === true && !process.env.NO_COLOR;

function wrap(code: number, text: string): string {
  return COLORS ? `\x1b[${code}m${text}\x1b[0m` : text;
}

const bold = (t: string) => wrap(1, t);
const dim = (t: string) => wrap(2, t);
const red = (t: string) => wrap(31, t);
const green = (t: string) => wrap(32, t);
const yellow = (t: string) => wrap(33, t);
const cyan = (t: string) => wrap(36, t);

function pad(text: string, width: number): string {
  return text.length >= width ? text : text + " ".repeat(width - text.length);
}

function humanAge(iso: string, now: number): string {
  const minutes = Math.floor((now - new Date(iso).getTime()) / 60_000);
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h`;
  if (minutes < 43200) return `${Math.floor(minutes / 1440)}d`;
  return `${Math.floor(minutes / 43200)}mo`;
}

type Deployment = {
  id: string;
  created_on: string;
  environment: string;
  url: string;
};

type ApiResponse<T> = {
  success: boolean;
  result: T;
  errors?: { message?: string }[];
};

const TABLE_HEADER = pad("ID", 8) + "  " + pad("DATE", 10) + "  " + pad("AGE", 4) + "  " + pad("ENV", 11) + "URL";
const TAG_WIDTH = 7;

function formatDeployment(d: Deployment, now: number): string {
  return (
    pad(d.id.slice(0, 8), 8) +
    "  " +
    pad(d.created_on.slice(0, 10), 10) +
    "  " +
    pad(humanAge(d.created_on, now), 4) +
    "  " +
    pad(d.environment, 11) +
    d.url
  );
}

function apiError(errors: ApiResponse<unknown>["errors"]): string {
  return errors?.map((e) => e.message).filter(Boolean).join("; ") || "unknown error";
}

type Spinner = { update(text: string): void; stop(finalText?: string): void };

function spinner(initialText: string): Spinner {
  if (!COLORS) {
    console.log(initialText);
    return { update: () => {}, stop: (final) => final && console.log(final) };
  }
  const frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  let i = 0;
  let text = initialText;
  const draw = () => process.stdout.write(`\r\x1b[2K${cyan(frames[i++ % frames.length])} ${text}`);
  draw();
  const timer = setInterval(draw, 80);
  return {
    update(next) {
      text = next;
    },
    stop(final) {
      clearInterval(timer);
      process.stdout.write(`\r\x1b[2K${final ?? text}\n`);
    },
  };
}

function parseCli() {
  try {
    return parseArgs({
      options: {
        project: { type: "string", short: "p", default: DEFAULT_PROJECT },
        jobs: { type: "string", short: "j", default: String(DEFAULT_JOBS) },
        "dry-run": { type: "boolean", default: false },
        help: { type: "boolean", short: "h", default: false },
      },
    }).values;
  } catch (err) {
    console.error(`Invalid arguments: ${(err as Error).message}`);
    console.error(USAGE);
    process.exit(1);
  }
}

const args = parseCli();
const PROJECT = args.project ?? DEFAULT_PROJECT;
const JOBS = Number(args.jobs ?? DEFAULT_JOBS);
const DRY_RUN = args["dry-run"] ?? false;

if (args.help) {
  console.log(USAGE);
  console.log("  -p, --project <name>   Pages project name");
  console.log("  -j, --jobs <count>     concurrent deletions");
  console.log("      --dry-run          list what would be deleted");
  console.log("  -h, --help             show this help");
  process.exit(0);
}

if (!PROJECT.trim()) {
  console.error("Project name cannot be empty.");
  process.exit(1);
}

if (!Number.isInteger(JOBS) || JOBS < 1) {
  console.error("Jobs must be a positive integer.");
  process.exit(1);
}

const token = process.env.CF_API_TOKEN;
const accountId = process.env.CF_ACCOUNT_ID;

if (!token || !accountId) {
  console.error("Missing CF_API_TOKEN or CF_ACCOUNT_ID env vars.");
  process.exit(1);
}

const base = `https://api.cloudflare.com/client/v4/accounts/${accountId}/pages/projects/${PROJECT}/deployments`;

const headers = {
  Authorization: `Bearer ${token}`,
};

function formatHttpError(res: Awaited<ReturnType<typeof fetch>>, text: string): string {
  let detail = res.statusText;
  try {
    const data = JSON.parse(text) as ApiResponse<unknown>;
    const msg = apiError(data.errors);
    if (msg !== "unknown error") detail = msg;
  } catch {
    // keep status text
  }
  if (detail.length > 100) detail = detail.slice(0, 97) + "...";
  return `${res.status} ${detail}`;
}

async function listAll(onProgress?: (count: number) => void): Promise<Deployment[]> {
  const all: Deployment[] = [];
  let page = 1;
  while (true) {
    const res = await fetch(`${base}?page=${page}&per_page=25`, { headers });
    const data: ApiResponse<Deployment[]> = await res.json();
    if (res.status === 401) {
      throw new Error("Unauthorized (401): the API token is invalid or expired. Create a new token with Pages:Edit permission.");
    }
    if (!res.ok || !data.success) {
      throw new Error(`List failed: ${res.status} ${apiError(data.errors)}`);
    }
    all.push(...data.result);
    onProgress?.(all.length);
    if (data.result.length < 25) break;
    page++;
  }
  return all;
}

// Returns an error message on failure, or null on success.
async function deleteDeployment(id: string): Promise<string | null> {
  const res = await fetch(`${base}/${id}?force=true`, { method: "DELETE", headers });
  if (!res.ok) return formatHttpError(res, await res.text());
  return null;
}

async function deleteAll(deployments: Deployment[]): Promise<{ deleted: number; failures: string[] }> {
  const total = deployments.length;
  const failures: string[] = [];
  let index = 0;
  let deleted = 0;
  const progress = spinner(`Deleting ${deleted}/${total}`);

  async function worker() {
    while (index < total) {
      const d = deployments[index++];
      const error = await deleteDeployment(d.id);
      if (error) {
        failures.push(`${pad(d.id.slice(0, 8), 8)}  ${error}`);
      } else {
        deleted++;
      }
      progress.update(`Deleting ${deleted + failures.length}/${total}`);
    }
  }

  await Promise.all(Array.from({ length: Math.min(JOBS, total) }, worker));
  progress.stop();
  return { deleted, failures };
}

async function main() {
  const started = performance.now();

  const mode = DRY_RUN ? yellow("dry run") : green("live");
  console.log(
    bold("cf-pages-cleanup") +
      dim(`  ·  project ${PROJECT}  ·  keep last ${KEEP}  ·  jobs ${JOBS}  ·  ${mode}`)
  );

  const fetching = spinner("Fetching deployments…");
  let deployments: Deployment[];
  try {
    deployments = await listAll((n) => fetching.update(`Fetching deployments… ${n} found`));
  } catch (err) {
    fetching.stop(red("✘ Fetch failed"));
    throw err;
  }
  fetching.stop(`Found ${bold(String(deployments.length))} deployments`);

  // API returns newest first; sort explicitly to be safe.
  deployments.sort(
    (a, b) => new Date(b.created_on).getTime() - new Date(a.created_on).getTime()
  );

  // Never delete the live production deployment, even if a burst of previews
  // pushes it out of the newest KEEP.
  const liveProduction = deployments.find((d) => d.environment === "production");
  const keepIds = new Set(deployments.slice(0, KEEP).map((d) => d.id));
  if (liveProduction) keepIds.add(liveProduction.id);

  const now = Date.now();
  const toDelete = deployments.filter((d) => !keepIds.has(d.id));

  console.log(`Keeping ${bold(String(deployments.length - toDelete.length))}, deleting ${bold(String(toDelete.length))}`);

  if (toDelete.length === 0) {
    console.log(green("Nothing to delete: already within the keep limit."));
    return;
  }

  if (DRY_RUN) {
    console.log(dim(TABLE_HEADER));
    for (const d of deployments) {
      const row = formatDeployment(d, now);
      const tag = keepIds.has(d.id) ? dim(pad("keep", TAG_WIDTH)) : yellow(pad("delete", TAG_WIDTH));
      console.log(tag + (keepIds.has(d.id) ? dim(row) : row));
    }
    console.log(`Dry run: nothing deleted.`);
    return;
  }

  const { deleted, failures } = await deleteAll(toDelete);

  if (failures.length) {
    console.error(red(`✘ ${failures.length} failed:`));
    for (const f of failures) console.error("  " + red(f));
  }

  const seconds = ((performance.now() - started) / 1000).toFixed(1);
  const summary = [green(`${deleted} deleted`), failures.length ? red(`${failures.length} failed`) : null]
    .filter(Boolean)
    .join(", ");
  console.log(`${green("✔")} ${summary} in ${dim(`${seconds}s`)}`);
  if (failures.length) process.exitCode = 1;
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
