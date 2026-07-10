#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const MANIFEST = path.join(ROOT, "package.json");

const EXPECTED_IDENTITY = {
  name: "sponsoredai-credits",
  publisher: "Sacud",
  displayName: "SAI Credits by Sacud",
  license: "AGPL-3.0-or-later",
  homepage: "https://sponsoredai.dev",
  repositoryType: "git",
  repositoryUrl: "https://github.com/TheSacud/sponsoredai-public.git",
  repositoryDirectory: "vscode-extension",
  bugsUrl: "https://github.com/TheSacud/sponsoredai-public/issues"
};

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function stringValue(value) {
  return typeof value === "string" ? value : "";
}

function requireEqual(problems, label, actual, expected) {
  if (actual !== expected) {
    problems.push(`${label} ${JSON.stringify(actual)} must be ${JSON.stringify(expected)}`);
  }
}

function changelogVersionHeadings(changelog) {
  return [...String(changelog).matchAll(/^##\s+([^\r\n]+)/gm)].map((match) => match[1].trim());
}

function findVersionContractProblems({ manifest, readme, changelog }) {
  const problems = [];
  const version = stringValue(manifest.version);

  if (!/^\d+\.\d+\.\d+$/.test(version)) {
    problems.push(`package.json version ${JSON.stringify(manifest.version)} is not plain X.Y.Z semver`);
  }

  requireEqual(problems, "package.json name", manifest.name, EXPECTED_IDENTITY.name);
  requireEqual(problems, "package.json publisher", manifest.publisher, EXPECTED_IDENTITY.publisher);
  requireEqual(problems, "package.json displayName", manifest.displayName, EXPECTED_IDENTITY.displayName);
  requireEqual(problems, "package.json license", manifest.license, EXPECTED_IDENTITY.license);
  requireEqual(problems, "package.json homepage", manifest.homepage, EXPECTED_IDENTITY.homepage);
  requireEqual(problems, "package.json repository.type", manifest.repository?.type, EXPECTED_IDENTITY.repositoryType);
  requireEqual(problems, "package.json repository.url", manifest.repository?.url, EXPECTED_IDENTITY.repositoryUrl);
  requireEqual(
    problems,
    "package.json repository.directory",
    manifest.repository?.directory,
    EXPECTED_IDENTITY.repositoryDirectory
  );
  requireEqual(problems, "package.json bugs.url", manifest.bugs?.url, EXPECTED_IDENTITY.bugsUrl);

  const scripts = manifest.scripts || {};
  if (scripts["version:check"] !== "node scripts/checkVersion.js") {
    problems.push("package.json scripts.version:check must run `node scripts/checkVersion.js`");
  }
  const smoke = scripts["package:smoke"] || "";
  if (!smoke.includes("npm run version:check")) {
    problems.push("package:smoke must include npm run version:check");
  }

  const headings = changelogVersionHeadings(changelog);
  if (version && !new RegExp(`^##\\s+${escapeRegex(version)}\\s*$`, "m").test(String(changelog))) {
    problems.push(`CHANGELOG.md must contain a top-level ## ${version} entry`);
  }
  if (version && headings.length > 0 && headings[0] !== version) {
    problems.push(
      `CHANGELOG.md top version ${JSON.stringify(headings[0])} must match package.json ${JSON.stringify(version)}`
    );
  }

  const marketplaceId = `${EXPECTED_IDENTITY.publisher}.${EXPECTED_IDENTITY.name}`;
  if (!String(readme).includes(marketplaceId)) {
    problems.push(`README.md must document the Marketplace id ${marketplaceId}`);
  }
  if (!String(readme).includes("npm install -g @sponsoredai/cli")) {
    problems.push("README.md must keep the visible CLI install command documented");
  }

  return problems;
}

function findVersionContractProblemsForRoot(root = ROOT) {
  return findVersionContractProblems({
    manifest: readJson(path.join(root, "package.json")),
    readme: fs.readFileSync(path.join(root, "README.md"), "utf8"),
    changelog: fs.readFileSync(path.join(root, "CHANGELOG.md"), "utf8")
  });
}

function main() {
  const problems = findVersionContractProblemsForRoot(ROOT);
  if (problems.length > 0) {
    console.error("VS Code extension version check FAILED:");
    for (const problem of problems) {
      console.error(`  [x] ${problem}`);
    }
    process.exitCode = 1;
    return;
  }

  const version = readJson(MANIFEST).version;
  console.log(`VS Code extension version check OK: ${EXPECTED_IDENTITY.publisher}.${EXPECTED_IDENTITY.name}@${version}.`);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`VS Code extension version check FAILED: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = {
  EXPECTED_IDENTITY,
  changelogVersionHeadings,
  findVersionContractProblems,
  findVersionContractProblemsForRoot
};
