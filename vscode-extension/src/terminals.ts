import * as os from "node:os";
import type * as vscode from "vscode";

export type SaiTerminalAction = "codex" | "claude" | "overlay" | "dashboard" | "installCli";

type TerminalSpec = {
  readonly name: string;
  readonly args: readonly string[];
  readonly command?: string;
  // When set, the terminal opens in this directory instead of the workspace.
  readonly cwd?: string;
};

export interface SaiTerminalCommandOptions {
  readonly saiCommand?: string;
  readonly launchCwd?: string;
  readonly platform?: NodeJS.Platform;
  readonly sendDelayMs?: number;
  readonly scheduleSend?: (callback: () => void, delayMs: number) => unknown;
}

export const WINDOWS_TERMINAL_SEND_DELAY_MS = 1200;

const TERMINAL_SPECS: Record<SaiTerminalAction, TerminalSpec> = {
  codex: {
    name: "SAI Codex",
    args: ["codex"]
  },
  claude: {
    name: "SAI Claude",
    args: ["claude"]
  },
  overlay: {
    name: "SAI Overlay",
    args: ["overlay", "both"]
  },
  dashboard: {
    name: "SAI Dashboard",
    args: ["dashboard"]
  },
  installCli: {
    name: "SAI CLI Install",
    command: "npm install -g @sponsoredai/cli",
    args: [],
    // Run the global install from the home directory so a malicious .npmrc
    // committed to the open workspace cannot redirect the registry or inject
    // install scripts. The user/global npm config still applies.
    cwd: os.homedir()
  }
};

export interface SaiTerminalLike {
  readonly name: string;
  readonly exitStatus?: vscode.TerminalExitStatus;
  show(preserveFocus?: boolean): void;
  sendText(text: string, addNewLine?: boolean): void;
}

export interface SaiTerminalWindow {
  readonly terminals: readonly SaiTerminalLike[];
  createTerminal(options: vscode.TerminalOptions): SaiTerminalLike;
}

export function terminalCommandFor(action: SaiTerminalAction, options: SaiTerminalCommandOptions = {}): string {
  const spec = TERMINAL_SPECS[action];
  if (spec.command) {
    return spec.command;
  }
  const platform = options.platform ?? process.platform;
  const command = options.saiCommand?.trim() || defaultSaiCommand(platform);
  return [quoteTerminalCommand(command, platform), ...spec.args].join(" ");
}

export function terminalNameFor(action: SaiTerminalAction): string {
  return TERMINAL_SPECS[action].name;
}

export function openSaiTerminal(
  windowApi: SaiTerminalWindow,
  action: SaiTerminalAction,
  options: SaiTerminalCommandOptions = {}
): SaiTerminalLike {
  const spec = TERMINAL_SPECS[action];
  const terminalOptions: vscode.TerminalOptions = { name: spec.name };
  if (spec.cwd) {
    terminalOptions.cwd = spec.cwd;
  } else if ((options.platform ?? process.platform) === "win32") {
    // The Python Environments extension auto-activates workspace venvs in newly
    // created visible terminals by typing Activate.ps1. Creating hidden and then
    // immediately showing is enough to skip that hook while keeping normal UX.
    terminalOptions.hideFromUser = true;
    if (options.launchCwd) {
      terminalOptions.cwd = os.homedir();
      terminalOptions.env = { SAI_LAUNCH_CWD: options.launchCwd };
    }
  }
  // Always open a fresh terminal. Reusing a live terminal and re-sending the
  // command injects it into whatever is still running there: a second "Start
  // Overlay" appended its command to a busy prompt and ran the garbled
  // "sai overlay sai overlay both", and "Start Claude" would type "sai claude"
  // into an active Claude session. A clean terminal always runs the command as
  // typed. installCli additionally needs this for its controlled home cwd.
  return windowApi.createTerminal(terminalOptions);
}

export function runSaiTerminalCommand(
  windowApi: SaiTerminalWindow,
  action: SaiTerminalAction,
  options: SaiTerminalCommandOptions = {}
): SaiTerminalLike {
  const terminal = openSaiTerminal(windowApi, action, options);
  terminal.show();
  const command = terminalCommandFor(action, options);
  const delayMs = Math.max(0, options.sendDelayMs ?? 0);
  if (delayMs > 0) {
    const schedule = options.scheduleSend ?? ((callback: () => void, ms: number) => setTimeout(callback, ms));
    schedule(() => terminal.sendText(command, true), delayMs);
  } else {
    terminal.sendText(command, true);
  }
  return terminal;
}

export function defaultSaiCommand(platform: NodeJS.Platform = process.platform): string {
  // npm installs a PowerShell shim (`sai.ps1`) and a cmd shim (`sai.cmd`) on
  // Windows. Prefer the cmd shim when the user has not configured a custom path
  // so SAI does not depend on the user's PowerShell execution policy.
  return platform === "win32" ? "sai.cmd" : "sai";
}

export function quoteTerminalCommand(command: string, platform: NodeJS.Platform = process.platform): string {
  if (/^[A-Za-z0-9_./:\\-]+$/.test(command)) {
    return command;
  }
  if (platform === "win32") {
    return `"${command.replace(/(["^%])/g, "^$1")}"`;
  }
  return `'${command.replace(/'/g, "'\\''")}'`;
}
