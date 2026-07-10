import assert from "node:assert/strict";
import test from "node:test";

type VersionContractInput = {
  manifest: Record<string, unknown>;
  readme: string;
  changelog: string;
};

const { findVersionContractProblems } = require("../../scripts/checkVersion.js") as {
  findVersionContractProblems(input: VersionContractInput): string[];
};

function validManifest(): Record<string, unknown> {
  return {
    name: "sponsoredai-credits",
    displayName: "SAI Credits by Sacud",
    publisher: "Sacud",
    version: "0.0.7",
    license: "AGPL-3.0-or-later",
    repository: {
      type: "git",
      url: "https://github.com/TheSacud/sponsoredai-public.git",
      directory: "vscode-extension"
    },
    homepage: "https://sponsoredai.dev",
    bugs: {
      url: "https://github.com/TheSacud/sponsoredai-public/issues"
    },
    scripts: {
      "version:check": "node scripts/checkVersion.js",
      "package:smoke": "npm run version:check && npm test && npm run package && npm run package:check"
    }
  };
}

function validInput(overrides: Partial<VersionContractInput> = {}): VersionContractInput {
  return {
    manifest: validManifest(),
    readme: "Install `Sacud.sponsoredai-credits` from VS Code.\n\nnpm install -g @sponsoredai/cli\n",
    changelog: "# Changelog\n\n## 0.0.7\n\n- Add extension version checks.\n\n## 0.0.6\n\n- Previous release.\n",
    ...overrides
  };
}

test("accepts the extension version contract", () => {
  assert.deepEqual(findVersionContractProblems(validInput()), []);
});

test("requires the package version to lead the changelog", () => {
  const manifest = validManifest();
  manifest.version = "0.0.8";

  const problems = findVersionContractProblems(validInput({ manifest }));

  assert.match(problems.join("\n"), /CHANGELOG\.md must contain a top-level ## 0\.0\.8 entry/);
  assert.match(problems.join("\n"), /CHANGELOG\.md top version "0\.0\.7" must match package\.json "0\.0\.8"/);
});

test("flags Marketplace identity drift", () => {
  const manifest = validManifest();
  manifest.publisher = "SomeoneElse";

  const problems = findVersionContractProblems(validInput({ manifest }));

  assert.match(problems.join("\n"), /package\.json publisher "SomeoneElse" must be "Sacud"/);
});

test("keeps the version check wired into package smoke", () => {
  const manifest = validManifest();
  manifest.scripts = {
    "version:check": "node scripts/checkVersion.js",
    "package:smoke": "npm test && npm run package && npm run package:check"
  };

  const problems = findVersionContractProblems(validInput({ manifest }));

  assert.match(problems.join("\n"), /package:smoke must include npm run version:check/);
});
