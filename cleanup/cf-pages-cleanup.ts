#!/usr/bin/env bun
// Deletes all Cloudflare Pages deployments for a project except the N most recent.
// Requires: CF_API_TOKEN, CF_ACCOUNT_ID env vars.

const PROJECT = "awesome-android-root";
const KEEP = 5;

const token = process.env.CF_API_TOKEN;
const accountId = process.env.CF_ACCOUNT_ID;

if (!token || !accountId) {
  console.error("Missing CF_API_TOKEN or CF_ACCOUNT_ID env vars.");
  process.exit(1);
}

const base = `https://api.cloudflare.com/client/v4/accounts/${accountId}/pages/projects/${PROJECT}/deployments`;

const headers = {
  Authorization: `Bearer ${token}`,
  "Content-Type": "application/json",
};

type Deployment = {
  id: string;
  created_on: string;
  environment: string;
  url: string;
};

async function listAll(): Promise<Deployment[]> {
  const all: Deployment[] = [];
  let page = 1;
  while (true) {
    const res = await fetch(`${base}?page=${page}&per_page=25`, { headers });
    if (!res.ok) {
      throw new Error(`List failed: ${res.status} ${await res.text()}`);
    }
    const data = await res.json();
    const results: Deployment[] = data.result ?? [];
    if (results.length === 0) break;
    all.push(...results);
    page++;
  }
  return all;
}

async function deleteDeployment(id: string, force = false) {
  const url = `${base}/${id}${force ? "?force=true" : ""}`;
  const res = await fetch(url, { method: "DELETE", headers });
  if (!res.ok) {
    const body = await res.text();
    console.error(`Failed to delete ${id}: ${res.status} ${body}`);
    return false;
  }
  return true;
}

async function main() {
  console.log(`Fetching deployments for ${PROJECT}...`);
  const deployments = await listAll();

  // API returns newest first; sort explicitly to be safe.
  deployments.sort(
    (a, b) => new Date(b.created_on).getTime() - new Date(a.created_on).getTime()
  );

  console.log(`Found ${deployments.length} deployments. Keeping ${KEEP} most recent.`);

  const toDelete = deployments.slice(KEEP);

  if (toDelete.length === 0) {
    console.log("Nothing to delete.");
    return;
  }

  for (const d of toDelete) {
    process.stdout.write(`Deleting ${d.id} (${d.created_on}, ${d.environment})... `);
    const ok = await deleteDeployment(d.id, true);
    console.log(ok ? "done" : "skipped");
  }

  console.log(`Cleanup complete. Deleted ${toDelete.length} deployments.`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
