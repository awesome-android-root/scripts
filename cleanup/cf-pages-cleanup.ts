#!/usr/bin/env bun
// Deletes all Cloudflare Pages deployments for a project except the N most recent.
// Requires: CF_API_TOKEN, CF_ACCOUNT_ID env vars.
// Usage: bun cf-pages-cleanup.ts [-p project] [-j jobs] [--dry-run]

import { parseArgs } from "node:util";

const KEEP = 5;
const DEFAULT_PROJECT = "awesome-android-root";
const DEFAULT_JOBS = 5;
const USAGE = "Usage: bun cf-pages-cleanup.ts [-p project] [-j jobs] [--dry-run]";

function parseCli() {
  try {
    return parseArgs({
      options: {
        project: { type: "string", short: "p", default: DEFAULT_PROJECT },
        jobs: { type: "string", short: "j", default: String(DEFAULT_JOBS) },
        "dry-run": { type: "boolean", default: false },
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

type Deployment = {
  id: string;
  created_on: string;
  environment: string;
  url: string;
};

type ApiResponse<T> = {
  success: boolean;
  result: T;
  errors?: { message: string }[];
};

async function listAll(): Promise<Deployment[]> {
  const all: Deployment[] = [];
  let page = 1;
  while (true) {
    const res = await fetch(`${base}?page=${page}&per_page=25`, { headers });
    const data: ApiResponse<Deployment[]> = await res.json();
    if (!res.ok || !data.success) {
      throw new Error(`List failed: ${res.status} ${JSON.stringify(data.errors ?? data)}`);
    }
    all.push(...data.result);
    if (data.result.length < 25) break;
    page++;
  }
  return all;
}

// Returns an error message on failure, or null on success.
async function deleteDeployment(id: string): Promise<string | null> {
  const res = await fetch(`${base}/${id}?force=true`, { method: "DELETE", headers });
  if (!res.ok) return `${res.status} ${await res.text()}`;
  return null;
}

async function deleteAll(deployments: Deployment[]): Promise<number> {
  let index = 0;
  let deleted = 0;

  async function worker() {
    while (index < deployments.length) {
      const d = deployments[index++];
      const error = await deleteDeployment(d.id);
      if (error) {
        console.log(`Failed ${d.id} (${d.created_on}, ${d.environment}): ${error}`);
      } else {
        deleted++;
        console.log(`Deleted ${d.id} (${d.created_on}, ${d.environment})`);
      }
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(JOBS, deployments.length) }, worker)
  );
  return deleted;
}

async function main() {
  console.log(`Fetching deployments for ${PROJECT}...`);
  const deployments = await listAll();

  // API returns newest first; sort explicitly to be safe.
  deployments.sort(
    (a, b) => new Date(b.created_on).getTime() - new Date(a.created_on).getTime()
  );

  // Never delete the live production deployment, even if a burst of previews
  // pushes it out of the newest KEEP.
  const liveProduction = deployments.find((d) => d.environment === "production");
  const toDelete = deployments
    .slice(KEEP)
    .filter((d) => d.id !== liveProduction?.id);

  console.log(`Found ${deployments.length} deployments. Keeping ${KEEP} most recent.`);

  if (toDelete.length === 0) {
    console.log("Nothing to delete.");
    return;
  }

  if (DRY_RUN) {
    for (const d of toDelete) {
      console.log(`Would delete ${d.id} (${d.created_on}, ${d.environment}) ${d.url}`);
    }
    console.log(`Dry run: ${toDelete.length} deployments would be deleted.`);
    return;
  }

  console.log(`Deleting with ${Math.min(JOBS, toDelete.length)} parallel jobs.`);
  const deleted = await deleteAll(toDelete);

  console.log(`Cleanup complete. Deleted ${deleted}/${toDelete.length} deployments.`);
  if (deleted !== toDelete.length) process.exitCode = 1;
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
