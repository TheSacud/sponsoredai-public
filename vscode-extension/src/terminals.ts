import * as os from "node:os";
import type * as vscode from "vscode";

export type SaiTerminalAction = "codex" | "claude" | "overlay" | "dashboard" | "installCli";

type TerminalSpec = {
  readonly name: string;
  readonly command: string;
  // When set, the terminal opens in this directory instead of the workspace.
  readonly cwd?: string;
};

const TERMINAL_SPECS: Record<SaiTerminalAction, TerminalSpec> = {
  codex: {
    name: "SAI Codex",
    command: "sai codex"
  },
  claude: {
    name: "SAI Claude",
    command: "sai claude"
  },
  overlay: {
    name: "SAI Overlay",
    command: "sai overlay both"
  },
  dashboard: {
    name: "SAI Dashboard",
    command: "sai dashboard"
  },
  installCli: {
    name: "SAI CLI Install",
    command: "npm install -g @sponsoredai/cli",
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

export function terminalCommandFor(action: SaiTerminalAction): string {
  return TERMINAL_SPECS[action].command;
}

export function terminalNameFor(action: SaiTerminalAction): string {
  return TERMINAL_SPECS[action].name;
}

export function openSaiTerminal(windowApi: SaiTerminalWindow, action: SaiTerminalAction): SaiTerminalLike {
  const spec = TERMINAL_SPECS[action];
  const options: vscode.TerminalOptions = spec.cwd ? { name: spec.name, cwd: spec.cwd } : { name: spec.name };
  // Always open a fresh terminal. Reusing a live terminal and re-sending the
  // command injects it into whatever is still running there: a second "Start
  // Overlay" appended its command to a busy prompt and ran the garbled
  // "sai overlay sai overlay both", and "Start Claude" would type "sai claude"
  // into an active Claude session. A clean terminal always runs the command as
  // typed. installCli additionally needs this for its controlled home cwd.
  return windowApi.createTerminal(options);
}

export function runSaiTerminalCommand(windowApi: SaiTerminalWindow, action: SaiTerminalAction): SaiTerminalLike {
  const terminal = openSaiTerminal(windowApi, action);
  terminal.show();
  terminal.sendText(terminalCommandFor(action), true);
  return terminal;
}
