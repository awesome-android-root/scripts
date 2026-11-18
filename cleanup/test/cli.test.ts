import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import pkg from "../package.json" with { type: "json" };

const CLI = fileURLToPath(new URL("../cf-pages-cleanup.ts", import.meta.url));

function run(args: string[]) {
  return spawnSync(process.execPath, [CLI, ...args], {
    encoding: "utf8",
    env: { ...process.env, CF_API_TOKEN: "", CF_ACCOUNT_ID: "" },
  });
}

test("--version prints the package version without credentials", () => {
  const result = run(["--version"]);
  assert.equal(result.status, 0);
  assert.equal(result.stdout.trim(), pkg.version);
});

test("--help prints cf-pc usage and exits 0", () => {
  const result = run(["--help"]);
  assert.equal(result.status, 0);
  assert.match(result.stdout, /Usage: cf-pc/);
  assert.doesNotMatch(result.stdout, /\x1b\[/);
});

test("missing project is a usage error", () => {
  const result = run(["--dry-run"]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /-p, --project/);
  assert.match(result.stderr, /Usage: cf-pc/);
});

test("blank project is a usage error", () => {
  const result = run(["--project="]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /-p, --project/);
});

test("valid arguments reach the credentials check", () => {
  const result = run(["-p", "demo"]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Missing CF_API_TOKEN or CF_ACCOUNT_ID/);
});

test("keep accepts zero", () => {
  const result = run(["-p", "demo", "--keep=0"]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Missing CF_API_TOKEN or CF_ACCOUNT_ID/);
});

test("non-integer keep is rejected", () => {
  const result = run(["-p", "demo", "--keep=abc"]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Keep must be/);
});

test("negative keep is rejected", () => {
  const result = run(["-p", "demo", "--keep=-1"]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Keep must be/);
});

test("empty keep is rejected", () => {
  const result = run(["-p", "demo", "--keep="]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Keep must be a non-negative integer/);
});

test("zero jobs is rejected", () => {
  const result = run(["-p", "demo", "--jobs=0"]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Jobs must be a positive integer/);
});

test("non-integer jobs is rejected", () => {
  const result = run(["-p", "demo", "--jobs=abc"]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Jobs must be a positive integer/);
});
