import { execFile } from "node:child_process";
import type { ChildProcess, ExecFileOptions } from "node:child_process";
import * as fs from "node:fs";
import * as http from "node:http";
import * as path from "node:path";

export const SAI_COMMAND = "sai";
export const SAI_WALLET_ARGS = ["wallet", "--json"] as const;
export const DEFAULT_WALLET_TIMEOUT_MS = 8000;

export type SaiCliErrorReason = "notFound" | "timeout" | "failed" | "invalidJson";

export class SaiCliError extends Error {
  public readonly reason: SaiCliErrorReason;
  public readonly stderr: string;
  public readonly exitCode?: string | number;

  public constructor(reason: SaiCliErrorReason, message: string, options: { stderr?: string; exitCode?: string | number } = {}) {
    super(message);
    this.name = "SaiCliError";
    this.reason = reason;
    this.stderr = options.stderr ?? "";
    this.exitCode = options.exitCode;
  }
}

export type ExecFileRunner = (
  file: string,
  args: string[],
  options: ExecFileOptions,
  callback: (error: NodeJS.ErrnoException | null, stdout: string | Buffer, stderr: string | Buffer) => void
) => ChildProcess;

export interface ReadSaiOptions {
  readonly command?: string;
  readonly timeoutMs?: number;
  readonly execFileRunner?: ExecFileRunner;
  readonly platform?: NodeJS.Platform;
  readonly safeCwd?: string;
  readonly env?: NodeJS.ProcessEnv;
  // Allowlist of directories the resolved `sai` executable must live under.
  // Defaults to standard npm-global / system locations so an attacker-supplied
  // `sai` in the open workspace is never auto-executed.
  readonly trustedDirs?: readonly string[];
  // Directories to search for `sai` on POSIX. Defaults to the entries of PATH.
  readonly pathDirs?: readonly string[];
  // Existence check, injectable for tests. Defaults to a real filesystem check.
  readonly fileExists?: (candidate: string) => boolean;
  // Canonical (symlink-resolved) path, injectable for tests. Defaults to a real
  // fs.realpathSync. Used to ensure a symlink in a trusted directory does not
  // point at an untrusted target.
  readonly realPath?: (candidate: string) => string;
  // When `command` is an explicit, user-configured path (the sai.cliPath
  // setting), trust that exact path instead of requiring it under the
  // allowlist. It must still be an absolute, existing file with no shell
  // metacharacters or "."/".." segments. Ignored for the default "sai".
  readonly trustCommand?: boolean;
}

export type ReadSaiWalletOptions = ReadSaiOptions;

// Characters that cmd.exe may interpret even inside a quoted argument (%VAR%,
// !DELAYED!, ^escape) plus chaining/redirection metacharacters and control
// characters. Parentheses and spaces are allowed: they appear in legitimate
// paths such as "C:\\Program Files (x86)\\..." and are literal inside quotes.
const UNSAFE_PATH_CHARS = /["%!^&|<>`\r\n\t]/;

export const SAI_PLACEMENT_NEXT_ARGS = ["placement", "next", "--json"] as const;
export const SAI_PLACEMENT_EVENT_ARGS = ["placement", "event", "--json"] as const;
export const VSCODE_WAIT_SURFACE = "vscode_ai_wait";
export const DEFAULT_PLACEMENT_TOOL = "codex";
export const SAI_PLACEMENT_TRANSPORT_FIELD = "sai_transport";
const DEFAULT_GATEWAY_PLACEMENT_TIMEOUT_MS = 800;
const MAX_GATEWAY_PLACEMENT_RESPONSE_BYTES = 128 * 1024;

export interface GatewayPlacementOptions {
  readonly enabled?: boolean;
  readonly host?: string;
  readonly port?: number;
  readonly timeoutMs?: number;
}

export interface PlacementTransportOptions extends ReadSaiOptions {
  readonly gateway?: GatewayPlacementOptions;
}

export interface SaiPlacementDiagnostics {
  readonly gatewayPlacementEndpointAvailable?: boolean;
  readonly fallbackToCliUsed: boolean;
  readonly lastGatewayPlacementError?: string;
  readonly lastCliPlacementError?: string;
}

const placementDiagnostics: {
  gatewayPlacementEndpointAvailable?: boolean;
  fallbackToCliUsed: boolean;
  lastGatewayPlacementError?: string;
  lastCliPlacementError?: string;
} = {
  fallbackToCliUsed: false
};

export function saiPlacementDiagnosticsSnapshot(): SaiPlacementDiagnostics {
  return { ...placementDiagnostics };
}

export async function readSaiWalletJson(options: ReadSaiOptions = {}): Promise<unknown> {
  return runSaiJsonCommand([...SAI_WALLET_ARGS], options);
}

export async function readSaiVersion(options: ReadSaiOptions = {}): Promise<string> {
  return (await runSaiTextCommand(["--version"], options)).trim();
}

export interface PlacementRequest {
  readonly tool?: string;
  readonly attended?: boolean;
}

export async function fetchSaiPlacement(request: PlacementRequest = {}, options: PlacementTransportOptions = {}): Promise<unknown> {
  return runPlacementWithGatewayFallback(
    () => fetchGatewayPlacement(request, options.gateway),
    () => fetchSaiPlacementViaCli(request, options),
    effectiveGatewayOptions(options),
    true
  );
}

function fetchSaiPlacementViaCli(request: PlacementRequest = {}, options: ReadSaiOptions = {}): Promise<unknown> {
  const args = [
    ...SAI_PLACEMENT_NEXT_ARGS,
    "--surface", VSCODE_WAIT_SURFACE,
    "--tool", request.tool ?? DEFAULT_PLACEMENT_TOOL
  ];
  if (request.attended) {
    args.push("--attended");
  }
  return runSaiJsonCommand(args, options);
}

export interface PlacementEventRequest {
  readonly event?: string;
  readonly visibleSeconds?: number;
  readonly attended?: boolean;
}

export async function recordSaiPlacementEvent(
  ticket: unknown,
  request: PlacementEventRequest = {},
  options: PlacementTransportOptions = {}
): Promise<unknown> {
  if (placementTransport(ticket) === "cli") {
    return runPlacementCliOnly(() => recordSaiPlacementEventViaCli(ticket, request, options));
  }
  return runPlacementWithGatewayFallback(
    () => recordGatewayPlacementEvent(ticket, request, options.gateway),
    () => recordSaiPlacementEventViaCli(ticket, request, options),
    effectiveGatewayOptions(options)
  );
}

function recordSaiPlacementEventViaCli(
  ticket: unknown,
  request: PlacementEventRequest = {},
  options: ReadSaiOptions = {}
): Promise<unknown> {
  const args = [
    ...SAI_PLACEMENT_EVENT_ARGS,
    "--event", request.event ?? "qualified_5s",
    "--visible-seconds", String(request.visibleSeconds ?? 0)
  ];
  if (request.attended) {
    args.push("--attended");
  }
  // The placement ticket (placement_id, signature, campaign_id, ...) goes on
  // stdin, never argv, so the short-lived signature cannot appear in a process
  // list on a shared machine.
  return runSaiJsonCommand(args, options, JSON.stringify(ticket ?? {}));
}

export function fetchGatewayPlacement(
  request: PlacementRequest = {},
  options: GatewayPlacementOptions = {}
): Promise<unknown> {
  return postGatewayPlacementJson(
    "/v1/sai/placements/next",
    {
      surface: VSCODE_WAIT_SURFACE,
      tool: request.tool ?? DEFAULT_PLACEMENT_TOOL,
      attended: Boolean(request.attended)
    },
    options
  );
}

export function recordGatewayPlacementEvent(
  ticket: unknown,
  request: PlacementEventRequest = {},
  options: GatewayPlacementOptions = {}
): Promise<unknown> {
  return postGatewayPlacementJson(
    "/v1/sai/placements/event",
    {
      ticket: ticket ?? {},
      event: request.event ?? "qualified_5s",
      visible_seconds: request.visibleSeconds ?? 0,
      attended: Boolean(request.attended)
    },
    options
  );
}

type GatewayPlacementErrorReason = "notFound" | "timeout" | "unreachable" | "invalidJson" | "responseTooLarge" | "failed";

class GatewayPlacementError extends Error {
  public readonly reason: GatewayPlacementErrorReason;
  public readonly statusCode?: number;

  public constructor(reason: GatewayPlacementErrorReason, message: string, statusCode?: number) {
    super(message);
    this.name = "GatewayPlacementError";
    this.reason = reason;
    this.statusCode = statusCode;
  }
}

async function runPlacementWithGatewayFallback(
  gatewayCall: () => Promise<unknown>,
  cliCall: () => Promise<unknown>,
  gatewayOptions: GatewayPlacementOptions | undefined,
  annotatePlacement = false
): Promise<unknown> {
  if (gatewayOptions?.enabled !== false) {
    try {
      const payload = await gatewayCall();
      placementDiagnostics.gatewayPlacementEndpointAvailable = true;
      placementDiagnostics.fallbackToCliUsed = false;
      placementDiagnostics.lastGatewayPlacementError = undefined;
      placementDiagnostics.lastCliPlacementError = undefined;
      return annotatePlacement ? annotatePlacementTransport(payload, "gateway") : payload;
    } catch (error) {
      placementDiagnostics.gatewayPlacementEndpointAvailable = false;
      placementDiagnostics.lastGatewayPlacementError = placementErrorSummary(error);
      if (!shouldFallbackFromGateway(error)) {
        placementDiagnostics.fallbackToCliUsed = false;
        throw error;
      }
    }
  }

  placementDiagnostics.fallbackToCliUsed = true;
  try {
    const payload = await cliCall();
    placementDiagnostics.lastCliPlacementError = undefined;
    return annotatePlacement ? annotatePlacementTransport(payload, "cli") : payload;
  } catch (error) {
    placementDiagnostics.lastCliPlacementError = placementErrorSummary(error);
    throw error;
  }
}

async function runPlacementCliOnly(cliCall: () => Promise<unknown>): Promise<unknown> {
  placementDiagnostics.fallbackToCliUsed = true;
  try {
    const payload = await cliCall();
    placementDiagnostics.lastCliPlacementError = undefined;
    return payload;
  } catch (error) {
    placementDiagnostics.lastCliPlacementError = placementErrorSummary(error);
    throw error;
  }
}

function effectiveGatewayOptions(options: PlacementTransportOptions): GatewayPlacementOptions | undefined {
  if (options.command) {
    return { ...options.gateway, enabled: false };
  }
  return options.gateway;
}

function annotatePlacementTransport(payload: unknown, transport: "gateway" | "cli"): unknown {
  if (!payload || typeof payload !== "object") {
    return payload;
  }
  const outer = payload as Record<string, unknown>;
  const placement = outer.placement;
  if (!placement || typeof placement !== "object") {
    return payload;
  }
  return {
    ...outer,
    placement: {
      ...(placement as Record<string, unknown>),
      [SAI_PLACEMENT_TRANSPORT_FIELD]: transport
    }
  };
}

function placementTransport(ticket: unknown): "gateway" | "cli" | undefined {
  if (!ticket || typeof ticket !== "object") {
    return undefined;
  }
  const value = (ticket as Record<string, unknown>)[SAI_PLACEMENT_TRANSPORT_FIELD];
  return value === "gateway" || value === "cli" ? value : undefined;
}

function shouldFallbackFromGateway(error: unknown): boolean {
  return error instanceof GatewayPlacementError
    && ["notFound", "timeout", "unreachable", "invalidJson", "responseTooLarge"].includes(error.reason);
}

function placementErrorSummary(error: unknown): string {
  if (error instanceof GatewayPlacementError) {
    return error.statusCode ? `${error.reason}:${error.statusCode}` : error.reason;
  }
  if (error instanceof SaiCliError) {
    return error.exitCode ? `${error.reason}:${error.exitCode}` : error.reason;
  }
  return error instanceof Error ? error.name : "failed";
}

function postGatewayPlacementJson(
  pathName: "/v1/sai/placements/next" | "/v1/sai/placements/event",
  payload: Record<string, unknown>,
  options: GatewayPlacementOptions
): Promise<unknown> {
  const host = normalizeGatewayHost(options.host);
  const port = normalizeGatewayPort(options.port);
  const timeout = options.timeoutMs ?? DEFAULT_GATEWAY_PLACEMENT_TIMEOUT_MS;
  const body = JSON.stringify(payload);

  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback: () => void): void => {
      if (settled) {
        return;
      }
      settled = true;
      callback();
    };
    const request = http.request(
      {
        host,
        port,
        path: pathName,
        method: "POST",
        timeout,
        headers: {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(body)
        }
      },
      (response) => {
        const statusCode = response.statusCode ?? 0;
        if (statusCode !== 200) {
          response.resume();
          const reason: GatewayPlacementErrorReason = statusCode === 404 ? "notFound" : "failed";
          finish(() => reject(new GatewayPlacementError(reason, "SAI gateway placement endpoint failed.", statusCode)));
          return;
        }

        let responseBody = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          responseBody += chunk;
          if (responseBody.length > MAX_GATEWAY_PLACEMENT_RESPONSE_BYTES) {
            request.destroy();
            finish(() => reject(new GatewayPlacementError("responseTooLarge", "SAI gateway placement response is too large.")));
          }
        });
        response.on("end", () => {
          finish(() => {
            try {
              resolve(JSON.parse(responseBody) as unknown);
            } catch {
              reject(new GatewayPlacementError("invalidJson", "SAI gateway placement endpoint returned invalid JSON."));
            }
          });
        });
      }
    );

    request.on("error", (error) => {
      finish(() => reject(classifyGatewayPlacementError(error)));
    });
    request.on("timeout", () => {
      request.destroy();
      finish(() => reject(new GatewayPlacementError("timeout", "SAI gateway placement endpoint timed out.")));
    });
    request.end(body);
  });
}

function classifyGatewayPlacementError(error: unknown): GatewayPlacementError {
  const errno = error as NodeJS.ErrnoException;
  if (errno.code === "ECONNREFUSED" || errno.code === "EHOSTUNREACH" || errno.code === "ENETUNREACH") {
    return new GatewayPlacementError("unreachable", "SAI gateway placement endpoint is unreachable.");
  }
  return new GatewayPlacementError("failed", "SAI gateway placement endpoint failed.");
}

async function runSaiJsonCommand(commandArgs: readonly string[], options: ReadSaiOptions, input?: string): Promise<unknown> {
  const stdout = await runSaiTextCommand(commandArgs, options, input);
  try {
    return JSON.parse(stdout) as unknown;
  } catch (error) {
    throw new SaiCliError("invalidJson", "SAI returned invalid JSON.", {
      stderr: error instanceof Error ? error.message : String(error)
    });
  }
}

async function runSaiTextCommand(commandArgs: readonly string[], options: ReadSaiOptions, input?: string): Promise<string> {
  const command = options.command ?? SAI_COMMAND;
  const timeout = options.timeoutMs ?? DEFAULT_WALLET_TIMEOUT_MS;
  const runner = options.execFileRunner ?? (execFile as ExecFileRunner);
  const platform = options.platform ?? process.platform;
  const env = options.env ?? process.env;
  const cwd = options.safeCwd ?? safeExecutionCwd();
  const fileExists = options.fileExists ?? realFileExists;
  const realPath = options.realPath ?? realCanonicalPath;
  const trusted = (options.trustedDirs ?? defaultTrustedDirs(platform, env)).filter(Boolean);
  const pathDirs = options.pathDirs ?? splitPathDirs(env, platform);

  const invocation = await resolveSaiInvocation({
    command,
    platform,
    runner,
    cwd,
    timeout,
    env,
    fileExists,
    realPath,
    trusted,
    pathDirs,
    commandArgs: [...commandArgs],
    trustCommand: options.trustCommand ?? false
  });

  return runExecFileText(
    runner,
    invocation.file,
    invocation.args,
    {
      timeout,
      windowsHide: true,
      maxBuffer: 128 * 1024,
      shell: false,
      cwd
    },
    input
  );
}

interface WalletInvocation {
  readonly file: string;
  readonly args: string[];
}

interface ResolveContext {
  readonly command: string;
  readonly platform: NodeJS.Platform;
  readonly runner: ExecFileRunner;
  readonly cwd: string;
  readonly timeout: number;
  readonly env: NodeJS.ProcessEnv;
  readonly fileExists: (candidate: string) => boolean;
  readonly realPath: (candidate: string) => string;
  readonly trusted: readonly string[];
  readonly pathDirs: readonly string[];
  readonly commandArgs: readonly string[];
  readonly trustCommand: boolean;
}

async function resolveSaiInvocation(ctx: ResolveContext): Promise<WalletInvocation> {
  const isDefaultCommand = ctx.command === SAI_COMMAND;
  // A configured cliPath is existence-checked (so a wrong setting fails clearly)
  // and trusted as-is; the default "sai" is existence-checked after resolution.
  const requireExists = isDefaultCommand || ctx.trustCommand;

  if (ctx.platform !== "win32") {
    const resolved = isDefaultCommand
      ? resolvePosixSai(ctx)
      : ctx.command;
    assertSafeExecutable(resolved, ctx, requireExists);
    return { file: resolved, args: [...ctx.commandArgs] };
  }

  const resolved = isDefaultCommand
    ? await resolveWindowsSaiOnPath(ctx)
    : ctx.command;
  assertSafeExecutable(resolved, ctx, requireExists);

  if (/\.(cmd|bat)$/i.test(resolved)) {
    // Pass the .cmd path and the command args as SEPARATE arguments. Pre-joining
    // them into one quoted string ("path" wallet --json) makes Node's Windows
    // arg escaping double-quote the whole thing, so cmd.exe sees a mangled
    // command and reports "is not recognized". As separate args, Node quotes
    // only the path (when needed) and cmd.exe parses it correctly, spaces too.
    return {
      file: windowsCmdExe(ctx.env),
      args: ["/d", "/s", "/c", resolved, ...ctx.commandArgs]
    };
  }

  return {
    file: resolved,
    args: [...ctx.commandArgs]
  };
}

async function resolveWindowsSaiOnPath(ctx: ResolveContext): Promise<string> {
  let stdout: string;
  try {
    stdout = await runExecFileText(ctx.runner, windowsWhereExe(ctx.env), [SAI_COMMAND], {
      timeout: Math.min(ctx.timeout, 2000),
      windowsHide: true,
      maxBuffer: 32 * 1024,
      shell: false,
      cwd: ctx.cwd
    });
  } catch (error) {
    throw classifyCliError(error, error instanceof SaiCliError ? error.stderr : "");
  }

  const candidates = stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  const safeCandidate = candidates.find((candidate) =>
    /\.(cmd|exe|bat)$/i.test(candidate)
    && isSafeCandidate(candidate, ctx.platform, ctx.trusted)
  );

  if (!safeCandidate) {
    throw new SaiCliError(
      "notFound",
      "SAI CLI was not found in a trusted install location. Install it with npm install -g @sponsoredai/cli or launch SAI from the integrated terminal."
    );
  }

  return safeCandidate;
}

// POSIX has no where.exe; resolve to an absolute path by scanning PATH in-process
// (the OS would otherwise resolve a bare name via PATH with no allowlist).
function resolvePosixSai(ctx: ResolveContext): string {
  for (const dir of ctx.pathDirs) {
    if (!dir) {
      continue;
    }
    const candidate = `${dir.replace(/\/+$/, "")}/${SAI_COMMAND}`;
    if (isSafeCandidate(candidate, ctx.platform, ctx.trusted) && ctx.fileExists(candidate)) {
      return candidate;
    }
  }

  throw new SaiCliError(
    "notFound",
    "SAI CLI was not found in a trusted install location. Install it with npm install -g @sponsoredai/cli or launch SAI from the integrated terminal."
  );
}

function assertSafeExecutable(resolved: string, ctx: ResolveContext, requireExists: boolean): void {
  // A user-configured cliPath (trustCommand) is trusted at that exact path; the
  // allowlist still applies to anything resolved from PATH / where.exe.
  const requireTrusted = !(ctx.command !== SAI_COMMAND && ctx.trustCommand);
  if (!isSafeCandidate(resolved, ctx.platform, ctx.trusted, requireTrusted)) {
    throw new SaiCliError(
      "notFound",
      "SAI CLI path is not a safe absolute path inside the trusted install locations. Install it with npm install -g @sponsoredai/cli, set sai.cliPath, or launch SAI from the integrated terminal."
    );
  }

  if (!requireExists) {
    return;
  }

  if (!ctx.fileExists(resolved)) {
    throw new SaiCliError("notFound", "SAI CLI was not found on disk.");
  }

  // A symlink may sit inside a trusted directory yet point outside it. Resolve
  // the real target and re-apply the same safety (and, for PATH-resolved
  // commands, the allowlist) so a planted symlink cannot smuggle execution out.
  let canonical: string;
  try {
    canonical = ctx.realPath(resolved);
  } catch {
    throw new SaiCliError("notFound", "SAI CLI could not be resolved to a real path.");
  }

  if (!isSafeCandidate(canonical, ctx.platform, ctx.trusted, requireTrusted)) {
    throw new SaiCliError(
      "notFound",
      "SAI CLI resolves to a target outside the trusted install locations."
    );
  }
}

function isSafeCandidate(
  candidate: string,
  platform: NodeJS.Platform,
  trusted: readonly string[],
  requireTrusted = true
): boolean {
  return isAbsoluteLike(candidate, platform)
    && !UNSAFE_PATH_CHARS.test(candidate)
    && !hasDotSegment(candidate)
    && (!requireTrusted || isWithinTrusted(candidate, trusted, platform));
}

// Reject any "." or ".." path segment. A "." is meaningless here and ".." would
// let a path like "<trusted>/../../evil/sai" textually match the allowlist while
// resolving outside it, so traversal segments are refused outright rather than
// silently collapsed (the OS would otherwise run the collapsed location).
function hasDotSegment(candidate: string): boolean {
  return candidate.split(/[\\/]+/).some((segment) => segment === "." || segment === "..");
}

function runExecFileText(
  runner: ExecFileRunner,
  file: string,
  args: string[],
  options: ExecFileOptions,
  input?: string
): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    try {
      const child = runner(file, args, options, (error, stdout, stderr) => {
        const stdoutText = stdout.toString();
        const stderrText = stderr.toString();

        if (error) {
          reject(classifyCliError(error, stderrText));
          return;
        }

        resolve(stdoutText);
      });
      if (input !== undefined && child && child.stdin) {
        // Best effort: an EPIPE if the child exits before reading stdin surfaces
        // through the exec callback's error, so swallow the write-side error here.
        child.stdin.on("error", () => undefined);
        child.stdin.end(input);
      }
    } catch (error) {
      reject(classifyCliError(error, ""));
    }
  });
}

function safeExecutionCwd(): string {
  return path.dirname(process.execPath);
}

export interface GatewayStatus {
  readonly activeRequests: number;
}

export interface GatewayStatusOptions {
  readonly host?: string;
  readonly port?: number;
  readonly timeoutMs?: number;
}

const LOOPBACK_GATEWAY_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

function normalizeGatewayHost(host: string | undefined): string {
  const candidate = (host ?? "127.0.0.1").trim().toLowerCase();
  return LOOPBACK_GATEWAY_HOSTS.has(candidate) ? candidate : "127.0.0.1";
}

function normalizeGatewayPort(port: number | undefined): number {
  return typeof port === "number" && Number.isInteger(port) && port > 0 && port <= 65_535 ? port : 8787;
}

// Poll the local gateway's in-flight count. Never rejects: an unreachable
// gateway (not running) resolves to undefined so the poller simply shows nothing.
export function readGatewayStatus(options: GatewayStatusOptions = {}): Promise<GatewayStatus | undefined> {
  const host = normalizeGatewayHost(options.host);
  const port = normalizeGatewayPort(options.port);
  const timeout = options.timeoutMs ?? 800;
  return new Promise((resolve) => {
    const request = http.get({ host, port, path: "/v1/status", timeout }, (response) => {
      if (response.statusCode !== 200) {
        response.resume();
        resolve(undefined);
        return;
      }
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
        if (body.length > 16 * 1024) {
          request.destroy();
        }
      });
      response.on("end", () => {
        try {
          const parsed = JSON.parse(body) as { active_requests?: unknown };
          const active = typeof parsed.active_requests === "number" && Number.isFinite(parsed.active_requests)
            ? parsed.active_requests
            : 0;
          resolve({ activeRequests: Math.max(0, Math.trunc(active)) });
        } catch {
          resolve(undefined);
        }
      });
    });
    request.on("error", () => resolve(undefined));
    request.on("timeout", () => {
      request.destroy();
      resolve(undefined);
    });
  });
}

function realFileExists(candidate: string): boolean {
  try {
    return fs.statSync(candidate).isFile();
  } catch {
    return false;
  }
}

function realCanonicalPath(candidate: string): string {
  return fs.realpathSync(candidate);
}

function splitPathDirs(env: NodeJS.ProcessEnv, platform: NodeJS.Platform): string[] {
  const raw = env.PATH ?? env.Path ?? "";
  const separator = platform === "win32" ? ";" : ":";
  return raw
    .split(separator)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function defaultTrustedDirs(platform: NodeJS.Platform, env: NodeJS.ProcessEnv): string[] {
  const dirs: Array<string | undefined> = [];
  const npmPrefix = env.npm_config_prefix ?? env.NPM_CONFIG_PREFIX;

  if (platform === "win32") {
    const appData = env.APPDATA;
    if (appData) {
      const npm = `${appData.replace(/[\\/]+$/, "")}\\npm`;
      // The bin shims and the global node_modules they may resolve into.
      dirs.push(npm, `${npm}\\node_modules`);
    }
    dirs.push(env.LOCALAPPDATA, env.ProgramFiles, env["ProgramFiles(x86)"], env.ProgramW6432, env.SystemRoot);
    if (npmPrefix) {
      const base = npmPrefix.replace(/[\\/]+$/, "");
      dirs.push(base, `${base}\\node_modules`);
    }
  } else {
    dirs.push(
      "/usr/local/bin",
      "/usr/bin",
      "/bin",
      "/usr/local/sbin",
      "/usr/sbin",
      "/opt/homebrew/bin",
      "/opt/homebrew/sbin",
      "/snap/bin",
      // Targets that the bin symlinks above commonly resolve into.
      "/usr/local/lib/node_modules",
      "/usr/lib/node_modules",
      "/opt/homebrew/lib/node_modules"
    );
    const home = env.HOME;
    if (home) {
      const base = home.replace(/\/+$/, "");
      dirs.push(
        `${base}/.npm-global/bin`,
        `${base}/.npm-global/lib/node_modules`,
        `${base}/.local/bin`,
        `${base}/.nvm`,
        `${base}/.volta/bin`,
        `${base}/.fnm`,
        `${base}/.asdf/shims`,
        `${base}/bin`
      );
    }
    if (npmPrefix) {
      const base = npmPrefix.replace(/\/+$/, "");
      dirs.push(base, `${base}/bin`, `${base}/lib/node_modules`);
    }
  }

  return dirs.filter((dir): dir is string => typeof dir === "string" && dir.length > 0 && isAbsoluteLike(dir, platform));
}

function isAbsoluteLike(candidate: string, platform: NodeJS.Platform): boolean {
  if (platform === "win32") {
    return /^[a-zA-Z]:[\\/]/.test(candidate) || /^\\\\/.test(candidate);
  }
  return candidate.startsWith("/");
}

function isWithinTrusted(candidate: string, trusted: readonly string[], platform: NodeJS.Platform): boolean {
  return trusted.some((dir) => isWithinDir(candidate, dir, platform));
}

function isWithinDir(child: string, dir: string, platform: NodeJS.Platform): boolean {
  if (!dir) {
    return false;
  }
  const separator = platform === "win32" ? "\\" : "/";
  const c = canonicalize(child, platform);
  const d = canonicalize(dir, platform);
  return c === d || c.startsWith(d + separator);
}

// String normalisation for comparison only: unify separators, lowercase on
// Windows, drop trailing separators. It deliberately does NOT collapse ".."
// segments - those are rejected up front by hasDotSegment so a traversal can
// never reach this comparison.
function canonicalize(value: string, platform: NodeJS.Platform): string {
  const separator = platform === "win32" ? "\\" : "/";
  let result = platform === "win32" ? value.replace(/\//g, "\\").toLowerCase() : value;
  while (result.length > 1 && result.endsWith(separator)) {
    result = result.slice(0, -1);
  }
  return result;
}

function windowsCmdExe(env: NodeJS.ProcessEnv): string {
  return windowsSystemTool(env, "cmd.exe");
}

function windowsWhereExe(env: NodeJS.ProcessEnv): string {
  return windowsSystemTool(env, "where.exe");
}

function windowsSystemTool(env: NodeJS.ProcessEnv, tool: "cmd.exe" | "where.exe"): string {
  const systemRoot = env.SystemRoot;
  if (
    systemRoot
    && isAbsoluteLike(systemRoot, "win32")
    && !UNSAFE_PATH_CHARS.test(systemRoot)
    && !hasDotSegment(systemRoot)
  ) {
    return `${systemRoot.replace(/[\\/]+$/, "")}\\System32\\${tool}`;
  }
  return `C:\\Windows\\System32\\${tool}`;
}

function classifyCliError(error: unknown, stderr: string): SaiCliError {
  const errno = error as NodeJS.ErrnoException & { killed?: boolean; signal?: string | null };
  const message = error instanceof Error ? error.message : String(error);
  const combined = `${message}\n${stderr}`.toLowerCase();

  if (errno.code === "ENOENT" || combined.includes("not recognized") || combined.includes("command not found")) {
    return new SaiCliError("notFound", "SAI CLI was not found on PATH.", {
      stderr,
      exitCode: errno.code
    });
  }

  if (errno.killed || errno.signal === "SIGTERM" || combined.includes("timed out")) {
    return new SaiCliError("timeout", "SAI wallet refresh timed out.", {
      stderr,
      exitCode: errno.code
    });
  }

  return new SaiCliError("failed", "SAI wallet refresh failed.", {
    stderr,
    exitCode: errno.code
  });
}
