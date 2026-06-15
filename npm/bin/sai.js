#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const packageRoot = path.resolve(__dirname, "..");
const binaryName = process.platform === "win32" ? "sai.exe" : "sai";

const PLATFORM_PACKAGES = new Map([
  ["darwin-arm64", "@sponsoredai/cli-darwin-arm64"],
  ["linux-x64", "@sponsoredai/cli-linux-x64"],
  ["win32-x64", "@sponsoredai/cli-win32-x64"]
]);

function platformKey(platform = process.platform, arch = process.arch) {
  return `${platform}-${arch}`;
}

function resolvePlatformBinary() {
  const key = platformKey();
  const packageName = PLATFORM_PACKAGES.get(key);
  if (!packageName) {
    throw new Error(`unsupported platform ${process.platform}/${process.arch}`);
  }

  try {
    const packageJson = require.resolve(`${packageName}/package.json`, {
      paths: [packageRoot]
    });
    const packageDir = path.dirname(packageJson);
    const candidate = path.join(packageDir, "bin", binaryName);
    if (fs.existsSync(candidate)) {
      return candidate;
    }
    throw new Error(`${packageName} is installed but ${path.relative(packageDir, candidate)} is missing`);
  } catch (error) {
    if (error && error.code !== "MODULE_NOT_FOUND") {
      throw error;
    }
  }

  const vendorFallback = path.join(packageRoot, "vendor", binaryName);
  if (fs.existsSync(vendorFallback)) {
    return vendorFallback;
  }

  throw new Error(
    `missing optional dependency ${packageName}. Reinstall @sponsoredai/cli without --omit=optional.`
  );
}

let binaryPath;
try {
  binaryPath = resolvePlatformBinary();
} catch (error) {
  console.error(`SAI binary is not installed: ${error.message}`);
  process.exit(1);
}

function startBinary(retriedAfterChmod = false) {
  const child = spawn(binaryPath, process.argv.slice(2), {
    stdio: "inherit",
    windowsHide: false
  });

  child.on("error", (error) => {
    if (error && error.code === "EACCES" && process.platform !== "win32" && !retriedAfterChmod) {
      try {
        fs.chmodSync(binaryPath, 0o755);
        startBinary(true);
        return;
      } catch (chmodError) {
        console.error(`Failed to make SAI executable: ${chmodError.message}`);
      }
    }

    console.error(`Failed to start SAI: ${error.message}`);
    process.exit(1);
  });

  child.on("close", (code, signal) => {
    if (signal) {
      console.error(`SAI exited from signal ${signal}`);
      process.exit(1);
    }
    process.exit(code == null ? 1 : code);
  });
}

startBinary();
