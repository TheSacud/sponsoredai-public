#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");

const ROOT = path.resolve(__dirname, "..");
const MANIFEST = path.join(ROOT, "package.json");

const REQUIRED_IGNORE_PATTERNS = [
  "src/**",
  "test/**",
  "out/test/**",
  "out/**/*.map",
  "node_modules/**",
  "package-lock.json",
  "*.vsix",
  "scripts/**",
  "vsix_unpack/**",
  ".vscode/**",
  "DEV_TESTING.md"
];

const REQUIRED_VSIX_ENTRIES = [
  "extension/package.json",
  "extension/out/src/extension.js",
  "extension/out/src/adBanner.js",
  "extension/out/src/saiCli.js",
  "extension/out/src/terminals.js",
  "extension/out/src/wallet.js",
  "extension/media/icon.png",
  "extension/media/sai-activitybar.svg",
  "extension/readme.md",
  "extension/changelog.md",
  "extension/LICENSE.txt"
];

const FORBIDDEN_ENTRY_PREFIXES = [
  "extension/node_modules/",
  "extension/src/",
  "extension/test/",
  "extension/out/test/",
  "extension/scripts/",
  "extension/.vscode/",
  "extension/vsix_unpack/"
];

const FORBIDDEN_ENTRY_NAMES = new Set([
  "extension/package-lock.json",
  "extension/DEV_TESTING.md"
]);

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function expectedVsixName(manifest) {
  return `${manifest.name}-${manifest.version}.vsix`;
}

function parseArgs(argv) {
  const options = { requireVsix: false, vsix: undefined };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--require-vsix") {
      options.requireVsix = true;
    } else if (arg === "--vsix") {
      index += 1;
      if (index >= argv.length) {
        throw new Error("--vsix requires a path");
      }
      options.vsix = path.resolve(argv[index]);
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  return options;
}

function checkSourceContract(root, manifest) {
  const problems = [];
  const scripts = manifest.scripts || {};

  if (!/^\d+\.\d+\.\d+$/.test(manifest.version || "")) {
    problems.push(`package.json version ${JSON.stringify(manifest.version)} is not plain X.Y.Z semver`);
  }
  if (manifest.main !== "./out/src/extension.js") {
    problems.push(`package.json main ${JSON.stringify(manifest.main)} must point at ./out/src/extension.js`);
  }
  const runtimeDeps = Object.keys(manifest.dependencies || {});
  if (runtimeDeps.length > 0) {
    problems.push(`runtime dependencies must stay empty; found ${runtimeDeps.join(", ")}`);
  }
  if (scripts.package !== "vsce package --no-dependencies") {
    problems.push("npm package script must run `vsce package --no-dependencies`");
  }
  if (!/node scripts\/checkPackage\.js --require-vsix/.test(scripts["package:check"] || "")) {
    problems.push("package:check must run the VSIX contract checker with --require-vsix");
  }
  if (scripts["version:check"] !== "node scripts/checkVersion.js") {
    problems.push("version:check must run the VS Code extension version contract checker");
  }
  const smoke = scripts["package:smoke"] || "";
  for (const token of ["npm run version:check", "npm test", "npm run package", "npm run package:check"]) {
    if (!smoke.includes(token)) {
      problems.push(`package:smoke must include ${token}`);
    }
  }

  const ignorePath = path.join(root, ".vscodeignore");
  const ignoreLines = fs.readFileSync(ignorePath, "utf8")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  const ignoreSet = new Set(ignoreLines);
  for (const pattern of REQUIRED_IGNORE_PATTERNS) {
    if (!ignoreSet.has(pattern)) {
      problems.push(`.vscodeignore must exclude ${pattern}`);
    }
  }

  return problems;
}

function findVsix(root, manifest, explicitPath) {
  if (explicitPath) {
    return explicitPath;
  }
  const expected = path.join(root, expectedVsixName(manifest));
  if (fs.existsSync(expected)) {
    return expected;
  }
  return undefined;
}

function parseZip(filePath) {
  const buffer = fs.readFileSync(filePath);
  const minEocd = 22;
  const searchStart = Math.max(0, buffer.length - minEocd - 0xffff);
  let eocd = -1;
  for (let offset = buffer.length - minEocd; offset >= searchStart; offset -= 1) {
    if (buffer.readUInt32LE(offset) === 0x06054b50) {
      eocd = offset;
      break;
    }
  }
  if (eocd < 0) {
    throw new Error("could not find ZIP end-of-central-directory record");
  }

  const totalEntries = buffer.readUInt16LE(eocd + 10);
  let offset = buffer.readUInt32LE(eocd + 16);
  const entries = new Map();

  for (let index = 0; index < totalEntries; index += 1) {
    if (buffer.readUInt32LE(offset) !== 0x02014b50) {
      throw new Error(`invalid ZIP central directory at offset ${offset}`);
    }
    const method = buffer.readUInt16LE(offset + 10);
    const compressedSize = buffer.readUInt32LE(offset + 20);
    const nameLength = buffer.readUInt16LE(offset + 28);
    const extraLength = buffer.readUInt16LE(offset + 30);
    const commentLength = buffer.readUInt16LE(offset + 32);
    const localHeaderOffset = buffer.readUInt32LE(offset + 42);
    const name = buffer.toString("utf8", offset + 46, offset + 46 + nameLength);
    entries.set(name, { method, compressedSize, localHeaderOffset });
    offset += 46 + nameLength + extraLength + commentLength;
  }

  return {
    entries,
    readEntry(name) {
      const entry = entries.get(name);
      if (!entry) {
        throw new Error(`ZIP entry not found: ${name}`);
      }
      const local = entry.localHeaderOffset;
      if (buffer.readUInt32LE(local) !== 0x04034b50) {
        throw new Error(`invalid ZIP local header for ${name}`);
      }
      const nameLength = buffer.readUInt16LE(local + 26);
      const extraLength = buffer.readUInt16LE(local + 28);
      const dataStart = local + 30 + nameLength + extraLength;
      const compressed = buffer.subarray(dataStart, dataStart + entry.compressedSize);
      if (entry.method === 0) {
        return Buffer.from(compressed);
      }
      if (entry.method === 8) {
        return zlib.inflateRawSync(compressed);
      }
      throw new Error(`unsupported ZIP compression method ${entry.method} for ${name}`);
    }
  };
}

function checkVsixPackage(vsixPath, sourceManifest) {
  const problems = [];
  if (!fs.existsSync(vsixPath)) {
    return [`VSIX not found: ${vsixPath}`];
  }

  const archive = parseZip(vsixPath);
  const names = [...archive.entries.keys()].sort();
  const nameSet = new Set(names);

  for (const entry of REQUIRED_VSIX_ENTRIES) {
    if (!nameSet.has(entry)) {
      problems.push(`VSIX missing required runtime file: ${entry}`);
    }
  }

  for (const name of names) {
    if (FORBIDDEN_ENTRY_NAMES.has(name)) {
      problems.push(`VSIX must not include ${name}`);
    }
    if (name.endsWith(".map") || name.endsWith(".vsix")) {
      problems.push(`VSIX must not include generated/debug artifact ${name}`);
    }
    for (const prefix of FORBIDDEN_ENTRY_PREFIXES) {
      if (name.startsWith(prefix)) {
        problems.push(`VSIX must not include ${name}`);
      }
    }
  }

  if (nameSet.has("extension/package.json")) {
    const packagedManifest = JSON.parse(archive.readEntry("extension/package.json").toString("utf8"));
    for (const key of ["name", "publisher", "version", "main"]) {
      if (packagedManifest[key] !== sourceManifest[key]) {
        problems.push(
          `packaged package.json ${key} ${JSON.stringify(packagedManifest[key])} `
            + `!= source ${JSON.stringify(sourceManifest[key])}`
        );
      }
    }
    const runtimeDeps = Object.keys(packagedManifest.dependencies || {});
    if (runtimeDeps.length > 0) {
      problems.push(`packaged runtime dependencies must stay empty; found ${runtimeDeps.join(", ")}`);
    }
  }

  return problems;
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  const manifest = readJson(MANIFEST);
  const problems = checkSourceContract(ROOT, manifest);
  const vsixPath = findVsix(ROOT, manifest, options.vsix);

  if (options.requireVsix && !vsixPath) {
    problems.push(`VSIX not found: expected ${expectedVsixName(manifest)} in ${ROOT}`);
  }
  if (vsixPath) {
    problems.push(...checkVsixPackage(vsixPath, manifest));
  }

  if (problems.length > 0) {
    console.error("VS Code extension package check FAILED:");
    for (const problem of problems) {
      console.error(`  [x] ${problem}`);
    }
    process.exitCode = 1;
    return;
  }

  const checked = vsixPath ? ` and ${path.basename(vsixPath)}` : "";
  console.log(`VS Code extension package check OK: source contract${checked}.`);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`VS Code extension package check FAILED: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = {
  checkSourceContract,
  checkVsixPackage,
  expectedVsixName,
  parseZip
};
