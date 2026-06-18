#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const TARGETS = {
  "darwin-arm64": {
    packageName: "@sponsoredai/cli-darwin-arm64",
    binaryName: "sai"
  },
  "linux-x64": {
    packageName: "@sponsoredai/cli-linux-x64",
    binaryName: "sai"
  },
  "win32-x64": {
    packageName: "@sponsoredai/cli-win32-x64",
    binaryName: "sai.exe"
  }
};

function fail(message) {
  console.error(`[sai] ${message}`);
  process.exit(1);
}

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    if (!name.startsWith("--")) {
      fail(`unexpected argument: ${name}`);
    }
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) {
      fail(`missing value for ${name}`);
    }
    args[name.slice(2)] = value;
    index += 1;
  }
  return args;
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function writeJson(file, payload) {
  fs.writeFileSync(file, `${JSON.stringify(payload, null, 2)}\n`);
}

function copyStagedBinary(source, binDir, target) {
  fs.rmSync(binDir, { recursive: true, force: true });
  fs.mkdirSync(binDir, { recursive: true });

  const sourceStat = fs.statSync(source);
  const destination = path.join(binDir, target.binaryName);
  if (sourceStat.isDirectory()) {
    fs.cpSync(source, binDir, { recursive: true });
  } else {
    fs.copyFileSync(source, destination);
  }

  if (!fs.existsSync(destination)) {
    fail(`staged package is missing ${path.relative(binDir, destination)}`);
  }
  if (!target.binaryName.endsWith(".exe")) {
    fs.chmodSync(destination, 0o755);
  }
  return destination;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const source = args.source && path.resolve(args.source);
  const targetName = args.target;
  if (!source || !targetName) {
    fail("usage: stage-platform-package.js --target <platform-arch> --source <binary>");
  }
  if (!fs.existsSync(source)) {
    fail(`source binary does not exist: ${source}`);
  }

  const target = TARGETS[targetName];
  if (!target) {
    fail(`unsupported target: ${targetName}`);
  }

  const npmRoot = path.resolve(__dirname, "..");
  const metaPackagePath = path.join(npmRoot, "package.json");
  const metaPackage = readJson(metaPackagePath);
  metaPackage.optionalDependencies = metaPackage.optionalDependencies || {};
  for (const platformTarget of Object.values(TARGETS)) {
    metaPackage.optionalDependencies[platformTarget.packageName] = metaPackage.version;
  }
  writeJson(metaPackagePath, metaPackage);

  const packageDir = path.join(npmRoot, "platform", targetName);
  const packageJsonPath = path.join(packageDir, "package.json");
  const platformPackage = readJson(packageJsonPath);

  if (platformPackage.name !== target.packageName) {
    fail(`expected ${packageJsonPath} to describe ${target.packageName}`);
  }

  platformPackage.version = metaPackage.version;
  writeJson(packageJsonPath, platformPackage);

  const binDir = path.join(packageDir, "bin");
  const destination = copyStagedBinary(source, binDir, target);

  console.log(`[sai] staged ${target.packageName}@${platformPackage.version}: ${destination}`);
}

main();
