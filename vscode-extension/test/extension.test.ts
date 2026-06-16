import assert from "node:assert/strict";
import type { ChildProcess, ExecFileOptions } from "node:child_process";
import * as fs from "node:fs";
import Module from "node:module";
import * as os from "node:os";
import * as path from "node:path";
import test from "node:test";
import type * as ExtensionModule from "../src/extension";
import { SaiCliError, readSaiWalletJson, type ExecFileRunner } from "../src/saiCli";
import { runSaiTerminalCommand, terminalCommandFor } from "../src/terminals";
import { formatWalletStatus, parseSaiWalletPayload, walletQuickPickItems } from "../src/wallet";

type Disposable = { dispose(): void };
type CommandCallback = (...args: unknown[]) => unknown;
type ExecCall = {
  readonly file: string;
  readonly args: string[];
  readonly options: ExecFileOptions;
};

class FakeTerminal {
  public readonly sent: string[] = [];
  public shown = false;
  public exitStatus = undefined;

  public constructor(public readonly name: string, public readonly cwd?: string) {}

  public show(): void {
    this.shown = true;
  }

  public sendText(text: string): void {
    this.sent.push(text);
  }
}

class FakeStatusBarItem {
  public text = "";
  public tooltip: string | undefined;
  public command: string | undefined;
  public shown = false;

  public show(): void {
    this.shown = true;
  }

  public dispose(): void {
    this.shown = false;
  }
}

function loadExtensionModule(): typeof ExtensionModule {
  const moduleWithLoader = Module as unknown as {
    _load(request: string, parent: unknown, isMain: boolean): unknown;
  };
  const originalLoad = moduleWithLoader._load;
  moduleWithLoader._load = function patchedLoad(request: string, parent: unknown, isMain: boolean): unknown {
    if (request === "vscode") {
      return {
        StatusBarAlignment: { Left: 1 },
        window: {},
        commands: {}
      };
    }
    return originalLoad.call(this, request, parent, isMain);
  };

  try {
    return require("../src/extension") as typeof ExtensionModule;
  } finally {
    moduleWithLoader._load = originalLoad;
  }
}

const extension = loadExtensionModule();

function readPackageManifest() {
  return JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "..", "package.json"), "utf8")
  ) as {
    version: string;
    activationEvents?: string[];
    contributes: {
      configuration: {
        properties: Record<string, { scope?: string }>;
      };
      commands: Array<{ command: string }>;
    };
  };
}

function createFakeApi() {
  const registeredCommands = new Map<string, CommandCallback>();
  const terminals: FakeTerminal[] = [];
  const statusBarItems: FakeStatusBarItem[] = [];
  const calls: {
    quickPickItems?: unknown[];
    warningMessage?: string;
    warningActions?: string[];
  } = {};
  let nextQuickPick: unknown;
  let nextWarning: string | undefined;

  const api = {
    StatusBarAlignment: { Left: 1 },
    commands: {
      registerCommand(command: string, callback: CommandCallback): Disposable {
        registeredCommands.set(command, callback);
        return {
          dispose(): void {
            registeredCommands.delete(command);
          }
        };
      }
    },
    window: {
      terminals,
      createTerminal(options: { name: string; cwd?: string }): FakeTerminal {
        const terminal = new FakeTerminal(options.name, options.cwd);
        terminals.push(terminal);
        return terminal;
      },
      createStatusBarItem(): FakeStatusBarItem {
        const item = new FakeStatusBarItem();
        statusBarItems.push(item);
        return item;
      },
      async showQuickPick(items: unknown[]): Promise<unknown> {
        calls.quickPickItems = items;
        return nextQuickPick;
      },
      async showWarningMessage(message: string, _options: unknown, ...actions: string[]): Promise<string | undefined> {
        calls.warningMessage = message;
        calls.warningActions = actions;
        return nextWarning;
      }
    }
  };

  return {
    api,
    registeredCommands,
    terminals,
    statusBarItems,
    calls,
    setNextQuickPick(value: unknown): void {
      nextQuickPick = value;
    },
    setNextWarning(value: string | undefined): void {
      nextWarning = value;
    }
  };
}

function createContext() {
  return { subscriptions: [] as Disposable[] };
}

function sampleWalletPayload(now = "2026-06-16T10:00:00.000Z") {
  return {
    balance: 3.42,
    backend_confirmed: true,
    local_wallet_authoritative: false,
    backend: {
      available_balance: 2.0,
      settled_balance: 0.5,
      pending_balance: 1.0,
      reconcile: {
        spendable_balance: 2.5,
        authoritative_balance: 3.5
      }
    },
    recent_entries: [
      {
        timestamp: now,
        kind: "earn",
        amount: 1.2,
        source: "sponsor:test"
      },
      {
        timestamp: "2026-06-15T23:30:00.000Z",
        kind: "spend",
        amount: -0.2,
        source: "gateway:test"
      }
    ]
  };
}

test("registers SAI commands on activation", () => {
  const fake = createFakeApi();
  const controller = extension.createExtensionController(fake.api as never, {
    readWalletJson: async () => sampleWalletPayload()
  });

  controller.activate(createContext() as never);

  for (const command of Object.values(extension.COMMANDS)) {
    assert.equal(fake.registeredCommands.has(command), true, `${command} should be registered`);
  }
});

test("manifest keeps sensitive settings machine-scoped and hides dev preview", () => {
  const manifest = readPackageManifest();
  const properties = manifest.contributes.configuration.properties;
  assert.equal(manifest.version, "0.0.1");
  assert.equal(properties["sai.cliPath"].scope, "machine");
  assert.equal(properties["sai.gateway.host"].scope, "machine");
  assert.equal(properties["sai.gateway.port"].scope, "machine");

  const commandIds = manifest.contributes.commands.map((command) => command.command);
  assert.equal(commandIds.includes("sai.previewSponsor"), false);
  assert.equal((manifest.activationEvents ?? []).includes("onCommand:sai.previewSponsor"), false);
});

test("plainStatusText strips codicon markup from sponsor names", () => {
  assert.equal(extension.plainStatusText("Acme $(zap) Tools"), "Acme ( zap) Tools");
});

test("generates fixed terminal commands and launches Codex terminal", async () => {
  assert.equal(terminalCommandFor("codex"), "sai codex");
  assert.equal(terminalCommandFor("claude"), "sai claude");
  assert.equal(terminalCommandFor("overlay"), "sai overlay both");
  assert.equal(terminalCommandFor("dashboard"), "sai dashboard");
  assert.equal(terminalCommandFor("installCli"), "npm install -g @sponsoredai/cli");

  const fake = createFakeApi();
  const controller = extension.createExtensionController(fake.api as never, {
    readWalletJson: async () => sampleWalletPayload()
  });
  controller.activate(createContext() as never);

  const command = fake.registeredCommands.get(extension.COMMANDS.startCodex);
  assert.ok(command);
  await command();

  assert.equal(fake.terminals.length, 1);
  assert.equal(fake.terminals[0].name, "SAI Codex");
  assert.equal(fake.terminals[0].shown, true);
  assert.deepEqual(fake.terminals[0].sent, ["sai codex"]);
});

test("opens a fresh terminal per launch instead of re-sending into a live one", () => {
  const stale = new FakeTerminal("SAI Overlay");
  const terminals: FakeTerminal[] = [stale];
  const created: FakeTerminal[] = [];
  const fakeWindow = {
    terminals,
    createTerminal(options: { name: string; cwd?: string }): FakeTerminal {
      const terminal = new FakeTerminal(options.name, options.cwd);
      created.push(terminal);
      terminals.push(terminal);
      return terminal;
    }
  };

  runSaiTerminalCommand(fakeWindow as never, "overlay");

  // The still-running terminal is left untouched - no command is injected into it.
  assert.deepEqual(stale.sent, []);
  assert.equal(stale.shown, false);
  assert.equal(created.length, 1);
  assert.equal(created[0].name, "SAI Overlay");
  assert.equal(created[0].shown, true);
  assert.deepEqual(created[0].sent, ["sai overlay both"]);
});

test("parses wallet JSON and surfaces the backend spendable balance as eligible", () => {
  const snapshot = parseSaiWalletPayload(sampleWalletPayload(), new Date("2026-06-16T12:00:00.000Z"));

  assert.equal(snapshot.localBalance, 3.42);
  assert.equal(snapshot.backendConfirmed, true);
  assert.ok(snapshot.backend);
  assert.equal(snapshot.backend?.spendable, 2.5);
  assert.equal(snapshot.backend?.pending, 1.0);
  assert.equal(snapshot.earnedToday, 1.2);
  assert.equal(snapshot.earnedTodayApproximate, false);
  // The eligible figure is the backend spendable balance, not the local display balance.
  assert.equal(formatWalletStatus(snapshot), "SAI: 2.500 credits eligible");

  const items = walletQuickPickItems(snapshot);
  assert.equal(items[0].label, "2.500 credits eligible");
  assert.equal(items[1].label, "1.000 credits pending");
  assert.equal(items[2].label, "3.420 credits local display balance");
  assert.equal(items[3].label, "1.200 credits earned today");
  assert.equal(items[4].label, "spend -0.200 credits");
  assert.equal(items[5].label, "earn +1.200 credits");
});

test("falls back to the local display balance and labels it unconfirmed without a backend summary", () => {
  const payload = {
    balance: 3.42,
    backend_confirmed: false,
    recent_entries: []
  };
  const snapshot = parseSaiWalletPayload(payload, new Date("2026-06-16T12:00:00.000Z"));

  assert.equal(snapshot.backend, undefined);
  assert.equal(formatWalletStatus(snapshot), "SAI: 3.420 credits (unconfirmed)");
  const items = walletQuickPickItems(snapshot);
  assert.equal(items[0].label, "3.420 credits local display balance");
  assert.match(items[0].description ?? "", /backend not confirmed/i);
});

test("marks earned today approximate when the ledger window is full", () => {
  const entries = Array.from({ length: 8 }, (_value, index) => ({
    timestamp: "2026-06-16T10:00:00.000Z",
    kind: "earn",
    amount: 1,
    source: `sponsor:${index}`
  }));
  const snapshot = parseSaiWalletPayload(
    { balance: 10, backend_confirmed: false, recent_entries: entries },
    new Date("2026-06-16T12:00:00.000Z")
  );

  assert.equal(snapshot.earnedToday, 8);
  assert.equal(snapshot.earnedTodayApproximate, true);
  const earnedItem = walletQuickPickItems(snapshot).find((item) => item.label.includes("earned today"));
  assert.match(earnedItem?.description ?? "", /approximate/i);
});

test("maps missing sai (no trusted candidate) to a notFound CLI error", async () => {
  await assert.rejects(
    () => readSaiWalletJson({ platform: "linux", pathDirs: [], trustedDirs: ["/usr/local/bin"] }),
    (error) => error instanceof SaiCliError && error.reason === "notFound"
  );
});

test("rejects a sai resolved outside the trusted install locations", async () => {
  await assert.rejects(
    () =>
      readSaiWalletJson({
        platform: "linux",
        pathDirs: ["/tmp/evil"],
        trustedDirs: ["/usr/local/bin"],
        fileExists: () => true
      }),
    (error) => error instanceof SaiCliError && error.reason === "notFound"
  );
});

test("resolves sai safely on Windows from a trusted location without a shell", async () => {
  const calls: ExecCall[] = [];
  const saiPath = "C:\\Users\\Duarte\\AppData\\Roaming\\npm\\sai.cmd";
  const execFileRunner: ExecFileRunner = (
    file: string,
    args: string[],
    options: ExecFileOptions,
    callback: (error: NodeJS.ErrnoException | null, stdout: string | Buffer, stderr: string | Buffer) => void
  ): ChildProcess => {
    calls.push({ file, args, options });
    if (/where\.exe$/i.test(file)) {
      queueMicrotask(() => callback(null, `${saiPath}\r\n`, ""));
      return {} as ChildProcess;
    }
    queueMicrotask(() => callback(null, JSON.stringify(sampleWalletPayload()), ""));
    return {} as ChildProcess;
  };

  await readSaiWalletJson({
    execFileRunner,
    platform: "win32",
    safeCwd: "C:\\Program Files\\Microsoft VS Code",
    trustedDirs: ["C:\\Users\\Duarte\\AppData\\Roaming\\npm"],
    fileExists: () => true,
    realPath: (candidate) => candidate
  });

  assert.equal(calls.length, 2);
  assert.match(calls[0].file, /\\System32\\where\.exe$/i);
  assert.deepEqual(calls[0].args, ["sai"]);
  assert.equal(calls[0].options.shell, false);
  assert.equal(calls[0].options.cwd, "C:\\Program Files\\Microsoft VS Code");
  assert.match(calls[1].file, /cmd\.exe$/i);
  assert.deepEqual(calls[1].args, ["/d", "/s", "/c", saiPath, "wallet", "--json"]);
  assert.equal(calls[1].options.shell, false);
});

test("rejects a Windows sai resolved outside trusted locations (workspace hijack)", async () => {
  const calls: ExecCall[] = [];
  const hostilePath = "C:\\Users\\victim\\workspace\\sai.cmd";
  const execFileRunner: ExecFileRunner = (
    file: string,
    _args: string[],
    _options: ExecFileOptions,
    callback: (error: NodeJS.ErrnoException | null, stdout: string | Buffer, stderr: string | Buffer) => void
  ): ChildProcess => {
    calls.push({ file, args: _args, options: _options });
    queueMicrotask(() => callback(null, `${hostilePath}\r\n`, ""));
    return {} as ChildProcess;
  };

  await assert.rejects(
    () =>
      readSaiWalletJson({
        execFileRunner,
        platform: "win32",
        safeCwd: "C:\\Program Files\\Microsoft VS Code",
        trustedDirs: ["C:\\Users\\Duarte\\AppData\\Roaming\\npm"],
        fileExists: () => true
      }),
    (error) => error instanceof SaiCliError && error.reason === "notFound"
  );
  // Only where.exe ran; the hostile sai.cmd was never invoked.
  assert.equal(calls.length, 1);
  assert.match(calls[0].file, /\\System32\\where\.exe$/i);
});

test("rejects a POSIX path that uses .. to escape a trusted directory", async () => {
  await assert.rejects(
    () =>
      readSaiWalletJson({
        platform: "linux",
        pathDirs: ["/usr/local/bin/../../tmp/evil"],
        trustedDirs: ["/usr/local/bin"],
        fileExists: () => true,
        realPath: (candidate) => candidate
      }),
    (error) => error instanceof SaiCliError && error.reason === "notFound"
  );
});

test("rejects a Windows path that uses .. to escape a trusted directory", async () => {
  const calls: ExecCall[] = [];
  const traversalPath = "C:\\Users\\Duarte\\AppData\\Roaming\\npm\\..\\..\\evil\\sai.cmd";
  const execFileRunner: ExecFileRunner = (
    file: string,
    args: string[],
    options: ExecFileOptions,
    callback: (error: NodeJS.ErrnoException | null, stdout: string | Buffer, stderr: string | Buffer) => void
  ): ChildProcess => {
    calls.push({ file, args, options });
    queueMicrotask(() => callback(null, `${traversalPath}\r\n`, ""));
    return {} as ChildProcess;
  };

  await assert.rejects(
    () =>
      readSaiWalletJson({
        execFileRunner,
        platform: "win32",
        safeCwd: "C:\\Program Files\\Microsoft VS Code",
        trustedDirs: ["C:\\Users\\Duarte\\AppData\\Roaming\\npm"],
        fileExists: () => true,
        realPath: (candidate) => candidate
      }),
    (error) => error instanceof SaiCliError && error.reason === "notFound"
  );
  assert.equal(calls.length, 1);
  assert.match(calls[0].file, /\\System32\\where\.exe$/i);
});

test("rejects a trusted-dir symlink whose real target is outside the trust boundary", async () => {
  await assert.rejects(
    () =>
      readSaiWalletJson({
        platform: "linux",
        pathDirs: ["/usr/local/bin"],
        trustedDirs: ["/usr/local/bin"],
        fileExists: () => true,
        // The bin entry is a symlink that resolves outside the allowlist.
        realPath: () => "/tmp/evil/sai"
      }),
    (error) => error instanceof SaiCliError && error.reason === "notFound"
  );
});

test("accepts a legitimate npm-global symlink that resolves into a trusted node_modules dir", async () => {
  let ran = false;
  const execFileRunner: ExecFileRunner = (
    _file: string,
    _args: string[],
    _options: ExecFileOptions,
    callback: (error: NodeJS.ErrnoException | null, stdout: string | Buffer, stderr: string | Buffer) => void
  ): ChildProcess => {
    ran = true;
    queueMicrotask(() => callback(null, JSON.stringify(sampleWalletPayload()), ""));
    return {} as ChildProcess;
  };

  const result = await readSaiWalletJson({
    execFileRunner,
    platform: "linux",
    pathDirs: ["/usr/local/bin"],
    trustedDirs: ["/usr/local/bin", "/usr/local/lib/node_modules"],
    fileExists: () => true,
    realPath: () => "/usr/local/lib/node_modules/@sponsoredai/cli/bin/sai.js"
  });

  assert.equal(ran, true);
  assert.equal((result as { balance: number }).balance, 3.42);
});

test("rejects invalid wallet JSON and timeout failures", async () => {
  const invalidJsonRunner: ExecFileRunner = (
    _file: string,
    _args: string[],
    _options: ExecFileOptions,
    callback: (error: NodeJS.ErrnoException | null, stdout: string | Buffer, stderr: string | Buffer) => void
  ): ChildProcess => {
    queueMicrotask(() => callback(null, "not-json", ""));
    return {} as ChildProcess;
  };

  await assert.rejects(
    () =>
      readSaiWalletJson({
        execFileRunner: invalidJsonRunner,
        platform: "linux",
        pathDirs: ["/usr/local/bin"],
        trustedDirs: ["/usr/local/bin"],
        fileExists: () => true,
        realPath: (candidate) => candidate
      }),
    (error) => error instanceof SaiCliError && error.reason === "invalidJson"
  );

  const timeoutRunner: ExecFileRunner = (
    _file: string,
    _args: string[],
    _options: ExecFileOptions,
    callback: (error: NodeJS.ErrnoException | null, stdout: string | Buffer, stderr: string | Buffer) => void
  ): ChildProcess => {
    const error = Object.assign(new Error("Command timed out"), { killed: true, signal: "SIGTERM" });
    queueMicrotask(() => callback(error, "", ""));
    return {} as ChildProcess;
  };

  await assert.rejects(
    () =>
      readSaiWalletJson({
        execFileRunner: timeoutRunner,
        platform: "linux",
        pathDirs: ["/usr/local/bin"],
        trustedDirs: ["/usr/local/bin"],
        fileExists: () => true,
        realPath: (candidate) => candidate
      }),
    (error) => error instanceof SaiCliError && error.reason === "timeout"
  );
});

test("status bar shows eligible, CLI-not-found, unavailable, and unreadable states", async () => {
  const fake = createFakeApi();
  const context = createContext();
  const controller = extension.createExtensionController(fake.api as never, {
    readWalletJson: async () => sampleWalletPayload()
  });
  controller.activate(context as never);
  await controller.refreshWalletStatus();
  assert.equal(fake.statusBarItems[0].text, "SAI: 2.500 credits eligible");

  const missingController = extension.createExtensionController(fake.api as never, {
    readWalletJson: async () => {
      throw new SaiCliError("notFound", "missing");
    }
  });
  missingController.activate(createContext() as never);
  await missingController.refreshWalletStatus();
  assert.equal(fake.statusBarItems[1].text, "SAI: CLI not found");

  const unavailableController = extension.createExtensionController(fake.api as never, {
    readWalletJson: async () => {
      throw new SaiCliError("failed", "failed", { stderr: "C:\\Users\\Duarte\\secret\\wallet.json" });
    }
  });
  unavailableController.activate(createContext() as never);
  await unavailableController.refreshWalletStatus();
  assert.equal(fake.statusBarItems[2].text, "SAI: wallet unavailable");
  assert.equal(fake.statusBarItems[2].tooltip?.includes("secret"), false);

  const unreadableController = extension.createExtensionController(fake.api as never, {
    readWalletJson: async () => ({ recent_entries: [] })
  });
  unreadableController.activate(createContext() as never);
  await unreadableController.refreshWalletStatus();
  assert.equal(fake.statusBarItems[3].text, "SAI: wallet unreadable");
});

test("a sponsor occupies the status bar during a wait and the wallet is restored after", async () => {
  const fake = createFakeApi();
  const controller = extension.createExtensionController(fake.api as never, {
    readWalletJson: async () => sampleWalletPayload()
  });
  controller.activate(createContext() as never);
  await controller.refreshWalletStatus();
  const bar = fake.statusBarItems[0];
  assert.equal(bar.text, "SAI: 2.500 credits eligible");

  controller.showSponsorStatus("$(megaphone) Acme", "Ship faster", extension.OPEN_SPONSOR_COMMAND);
  assert.equal(bar.text, "$(megaphone) Acme");
  assert.equal(bar.command, extension.OPEN_SPONSOR_COMMAND);

  // A wallet refresh while the sponsor is shown must not clobber the ad.
  await controller.refreshWalletStatus();
  assert.equal(bar.text, "$(megaphone) Acme");

  controller.clearSponsorStatus();
  assert.equal(bar.text, "SAI: 2.500 credits eligible");
  assert.equal(bar.command, extension.COMMANDS.showMenu);
});

test("a stale wallet refresh does not overwrite a newer result", async () => {
  const fake = createFakeApi();
  let resolveSlow: ((value: unknown) => void) | undefined;
  let call = 0;
  const controller = extension.createExtensionController(fake.api as never, {
    readWalletJson: () => {
      call += 1;
      if (call === 1) {
        // First (slow) refresh: resolves only after the second one finishes.
        return new Promise((resolve) => {
          resolveSlow = resolve;
        });
      }
      return Promise.resolve(sampleWalletPayload());
    }
  });
  controller.activate(createContext() as never);

  const slow = controller.refreshWalletStatus();
  const fast = controller.refreshWalletStatus();
  await fast;
  const statusBar = fake.statusBarItems[0];
  assert.equal(statusBar.text, "SAI: 2.500 credits eligible");

  // Now let the older refresh complete; it must not clobber the newer status.
  resolveSlow?.(sampleWalletPayload());
  await slow;
  assert.equal(statusBar.text, "SAI: 2.500 credits eligible");
});

test("showMenu displays actions and dispatches the chosen one", async () => {
  const fake = createFakeApi();
  fake.setNextQuickPick({ label: "Start Codex", action: "codex" });
  const controller = extension.createExtensionController(fake.api as never, {
    readWalletJson: async () => sampleWalletPayload()
  });
  controller.activate(createContext() as never);

  const showMenu = fake.registeredCommands.get(extension.COMMANDS.showMenu);
  assert.ok(showMenu);
  await showMenu();

  const labels = (fake.calls.quickPickItems as Array<{ label: string }>).map((item) => item.label);
  assert.deepEqual(labels, [
    "Start Codex",
    "Start Claude",
    "Start Overlay",
    "Wallet",
    "Open Dashboard",
    "Install / Update CLI"
  ]);
  assert.equal(fake.terminals.length, 1);
  assert.equal(fake.terminals[0].name, "SAI Codex");
  assert.deepEqual(fake.terminals[0].sent, ["sai codex"]);
});

test("Install CLI asks for confirmation and then opens a visible terminal", async () => {
  const fake = createFakeApi();
  fake.setNextWarning("Open Terminal");
  const controller = extension.createExtensionController(fake.api as never, {
    readWalletJson: async () => sampleWalletPayload()
  });
  controller.activate(createContext() as never);

  const command = fake.registeredCommands.get(extension.COMMANDS.installCli);
  assert.ok(command);
  await command();

  assert.match(fake.calls.warningMessage ?? "", /opens a visible integrated terminal/);
  assert.match(fake.calls.warningMessage ?? "", /\.npmrc/);
  assert.deepEqual(fake.calls.warningActions, ["Open Terminal"]);
  assert.equal(fake.terminals[0].name, "SAI CLI Install");
  assert.equal(fake.terminals[0].cwd, os.homedir());
  assert.deepEqual(fake.terminals[0].sent, ["npm install -g @sponsoredai/cli"]);
});

test("Install CLI always creates a fresh terminal with the controlled cwd", () => {
  const fake = createFakeApi();
  const staleInstallTerminal = new FakeTerminal("SAI CLI Install", "C:\\Users\\Duarte\\Documents\\Tokenback");
  fake.terminals.push(staleInstallTerminal);

  runSaiTerminalCommand(fake.api.window as never, "installCli");

  assert.equal(fake.terminals.length, 2);
  assert.deepEqual(staleInstallTerminal.sent, []);
  assert.equal(fake.terminals[1].name, "SAI CLI Install");
  assert.equal(fake.terminals[1].cwd, os.homedir());
  assert.deepEqual(fake.terminals[1].sent, ["npm install -g @sponsoredai/cli"]);
});

test("Install CLI cancellation does not open a terminal", async () => {
  const fake = createFakeApi();
  fake.setNextWarning(undefined);
  const controller = extension.createExtensionController(fake.api as never, {
    readWalletJson: async () => sampleWalletPayload()
  });
  controller.activate(createContext() as never);

  const command = fake.registeredCommands.get(extension.COMMANDS.installCli);
  assert.ok(command);
  await command();

  assert.equal(fake.terminals.length, 0);
});
