import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import pkg from "../package.json" with { type: "json" };

const CLI = fileURLToPath(new URL("../dist/cf-pc.js", import.meta.url));

function run(args: string[]) {
  return spawnSync(process.execPath, [CLI, ...args], {
    encoding: "utf8",
    env: { ...process.env, CF_API_TOKEN: "", CF_ACCOUNT_ID: "" },
  });
}

test("--version prints the package version", () => {
  const result = run(["--version"]);
  assert.equal(result.status, 0);
  assert.equal(result.stdout.trim(), pkg.version);
});

test("missing project is a usage error", () => {
  const result = run([]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /-p, --project/);
  assert.match(result.stderr, /Usage: cf-pc/);
});

test("--dry-run without project is a usage error", () => {
  const result = run(["--dry-run"]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /-p, --project/);
});

test("--help prints usage and exits 0", () => {
  const result = run(["--help"]);
  assert.equal(result.status, 0);
  assert.match(result.stdout, /Usage: cf-pc/);
});

test("built bin starts with a node shebang", () => {
  const source = readFileSync(CLI, "utf8");
  assert.ok(source.startsWith("#!/usr/bin/env node"));
});
