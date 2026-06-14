#!/usr/bin/env node
"use strict";

const SUPPORTED_TARGETS = new Set([
  "darwin-arm64",
  "linux-x64",
  "win32-x64"
]);

const target = `${process.platform}-${process.arch}`;

if (!SUPPORTED_TARGETS.has(target)) {
  console.error(
    `@sponsoredai/cli does not ship a binary for ${process.platform}/${process.arch}. ` +
    "Supported targets: macOS arm64, Linux x64, Windows x64."
  );
  process.exit(1);
}
