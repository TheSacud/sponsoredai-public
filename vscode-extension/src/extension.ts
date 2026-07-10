import * as vscode from "vscode";
import {
  DEFAULT_PLACEMENT_TOOL,
  fetchSaiPlacement,
  readSaiVersion,
  readGatewayStatus,
  readSaiWalletJson,
  recordSaiPlacementEvent,
  SaiCliError,
  saiPlacementDiagnosticsSnapshot,
  type GatewayStatus,
  type GatewayStatusOptions,
  type PlacementEventRequest,
  type PlacementRequest,
  type PlacementTransportOptions,
  type ReadSaiOptions
} from "./saiCli";
import { runSaiTerminalCommand, WINDOWS_TERMINAL_SEND_DELAY_MS, type SaiTerminalCommandOptions } from "./terminals";
import { AdEngine, parsePlacement, safeHttpsUrl, SaiAdViewProvider, type SponsorPlacement } from "./adBanner";
import {
  formatWalletStatus,
  parseSaiWalletPayload,
  walletQuickPickItems,
  walletTooltip,
  WalletParseError,
  type WalletSnapshot
} from "./wallet";

// Registered by the ad subsystem (in the real activate, not the unit-tested
// controller.activate), so it is intentionally outside COMMANDS.
export const OPEN_SPONSOR_COMMAND = "sai.openSponsor";
// How recently the user must have interacted for a wait to count as attended.
const ATTENDED_INPUT_WINDOW_MS = 30_000;
const AD_POLL_INTERVAL_MS = 1_000;
const LOOPBACK_GATEWAY_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

export function terminalSendDelayMs(platform: NodeJS.Platform = process.platform): number {
  return platform === "win32" ? WINDOWS_TERMINAL_SEND_DELAY_MS : 0;
}

export function terminalLaunchCwd(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function machineConfigValue<T>(config: vscode.WorkspaceConfiguration, section: string): T | undefined {
  const inspected = config.inspect<T>(section);
  return inspected?.globalValue ?? inspected?.defaultValue;
}

// Reads sai.cliPath: when set, the extension runs that exact `sai` launcher
// (for development against the source CLI, or a non-standard install) instead
// of resolving it from PATH. Only machine/global settings are honored so a
// cloned workspace cannot redirect the automatic wallet/placement commands.
function saiCliOptions(): ReadSaiOptions {
  const config = vscode.workspace.getConfiguration("sai");
  const cliPath = (machineConfigValue<string>(config, "cliPath") ?? "").trim();
  return cliPath ? { command: cliPath, trustCommand: true } : {};
}

// Strip VS Code codicon markup from untrusted text: StatusBarItem.text parses
// $(name) into an icon, so a sponsor name must not be able to inject one.
export function plainStatusText(value: string): string {
  return value.replace(/\$\(/g, "( ");
}

function gatewayStatusOptions(): GatewayStatusOptions {
  const config = vscode.workspace.getConfiguration("sai");
  const rawHost = (machineConfigValue<string>(config, "gateway.host") ?? "").trim().toLowerCase();
  const port = machineConfigValue<number>(config, "gateway.port");
  const host = LOOPBACK_GATEWAY_HOSTS.has(rawHost) ? rawHost : undefined;
  return {
    host,
    port: typeof port === "number" && Number.isInteger(port) && port > 0 && port <= 65_535 ? port : undefined
  };
}

function saiPlacementOptions(): PlacementTransportOptions {
  return {
    ...saiCliOptions(),
    gateway: gatewayStatusOptions()
  };
}

export const COMMANDS = {
  showMenu: "sai.showMenu",
  startCodex: "sai.startCodex",
  startClaude: "sai.startClaude",
  startOverlay: "sai.startOverlay",
  wallet: "sai.wallet",
  refreshWallet: "sai.refreshWallet",
  openDashboard: "sai.openDashboard",
  installCli: "sai.installCli",
  diagnostics: "sai.diagnostics"
} as const;

type VscodeApi = Pick<typeof vscode, "StatusBarAlignment" | "commands" | "window">;
type AdBannerVscodeApi = Pick<
  typeof vscode,
  "TextEditorSelectionChangeKind" | "Uri" | "commands" | "env" | "window"
>;

export interface SaiCliReader {
  readWalletJson(): Promise<unknown>;
  readVersion?(): Promise<string>;
}

type MenuAction = "codex" | "claude" | "overlay" | "wallet" | "dashboard" | "installCli" | "diagnostics";

interface MenuItem extends vscode.QuickPickItem {
  readonly action: MenuAction;
}

const DEFAULT_CLI: SaiCliReader = {
  readWalletJson: readSaiWalletJson,
  readVersion: readSaiVersion
};

type SaiTerminalOptionsProvider = () => SaiTerminalCommandOptions;
type PlacementTool = "codex" | "claude";
type CommandCallback = (...args: unknown[]) => unknown;

export interface SaiExtensionRuntime {
  nowMs(): number;
}

export interface SaiCommandPerformance {
  readonly command: string;
  readonly count: number;
  readonly lastDurationMs: number;
  readonly maxDurationMs: number;
  readonly lastCompletedAtMs: number;
}

export interface SaiExtensionPerformanceSnapshot {
  readonly activationSetupDurationMs?: number;
  readonly commandCount: number;
  readonly lastCommand?: SaiCommandPerformance;
  readonly slowestCommand?: SaiCommandPerformance;
}

const DEFAULT_EXTENSION_RUNTIME: SaiExtensionRuntime = {
  nowMs: () => Date.now()
};

function elapsedDurationMs(startMs: number, endMs: number): number {
  const duration = endMs - startMs;
  if (!Number.isFinite(duration) || duration < 0) {
    return 0;
  }
  return Math.round(duration * 10) / 10;
}

export function formatDurationMs(durationMs: number | undefined): string {
  if (durationMs === undefined) {
    return "not measured";
  }
  const value = Number.isInteger(durationMs) ? durationMs.toFixed(0) : durationMs.toFixed(1);
  return `${value} ms`;
}

function isPromiseLike(value: unknown): value is PromiseLike<unknown> {
  return (
    (typeof value === "object" || typeof value === "function")
    && value !== null
    && typeof (value as { then?: unknown }).then === "function"
  );
}

function copyCommandPerformance(value: SaiCommandPerformance | undefined): SaiCommandPerformance | undefined {
  return value ? { ...value } : undefined;
}

function formatCommandPerformance(
  value: SaiCommandPerformance | undefined,
  durationMs: number | undefined = value?.lastDurationMs
): string {
  if (!value) {
    return "none";
  }
  return `${value.command}: ${formatDurationMs(durationMs)} (${value.count} run${value.count === 1 ? "" : "s"})`;
}

interface AdBannerRuntime {
  nowMs(): number;
  setInterval(callback: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
  readGatewayStatus(options: GatewayStatusOptions): Promise<GatewayStatus | undefined>;
  fetchPlacement(request: PlacementRequest, options: PlacementTransportOptions): Promise<unknown>;
  recordPlacementEvent(
    placement: SponsorPlacement,
    request: PlacementEventRequest,
    options: PlacementTransportOptions
  ): Promise<unknown>;
  gatewayStatusOptions(): GatewayStatusOptions;
  placementOptions(): PlacementTransportOptions;
}

const DEFAULT_AD_BANNER_RUNTIME: AdBannerRuntime = {
  nowMs: () => Date.now(),
  setInterval: (callback, ms) => setInterval(callback, ms),
  clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
  readGatewayStatus,
  fetchPlacement: fetchSaiPlacement,
  recordPlacementEvent: recordSaiPlacementEvent,
  gatewayStatusOptions,
  placementOptions: saiPlacementOptions
};

export class SaiExtensionController {
  private statusBarItem?: vscode.StatusBarItem;
  // Monotonic id for the latest in-flight refresh. A slower earlier request must
  // not overwrite the status bar after a newer request has already updated it.
  private refreshSeq = 0;
  // While a sponsor occupies the status bar (during an attended wait), wallet
  // refreshes are remembered but not painted, so they cannot clobber the ad.
  private sponsorActive = false;
  private lastWalletText?: string;
  private lastWalletTooltip?: string;
  // The latest version we have already nudged about, so a passive update toast
  // shows once per new version per session instead of on every wallet refresh.
  private notifiedUpdateVersion?: string;
  private placementTool: PlacementTool = DEFAULT_PLACEMENT_TOOL;
  private activationSetupDurationMs?: number;
  private readonly commandPerformance = new Map<string, SaiCommandPerformance>();
  private lastCommandPerformance?: SaiCommandPerformance;

  public constructor(
    private readonly api: VscodeApi,
    private readonly cli: SaiCliReader = DEFAULT_CLI,
    private readonly terminalOptions: SaiTerminalOptionsProvider = () => ({}),
    private readonly runtime: SaiExtensionRuntime = DEFAULT_EXTENSION_RUNTIME
  ) {}

  public activate(context: vscode.ExtensionContext): void {
    const startedAtMs = this.runtime.nowMs();
    this.statusBarItem = this.api.window.createStatusBarItem(this.api.StatusBarAlignment.Left, 100);
    this.statusBarItem.command = COMMANDS.showMenu;
    this.statusBarItem.text = "SAI: checking...";
    this.statusBarItem.tooltip = "Reading the SAI wallet...";
    // Seed the remembered wallet state so a sponsor shown before the first
    // wallet read still has something to restore to when it clears.
    this.lastWalletText = this.statusBarItem.text;
    this.lastWalletTooltip = this.statusBarItem.tooltip;
    this.statusBarItem.show();
    context.subscriptions.push(this.statusBarItem);

    this.registerCommand(context, COMMANDS.showMenu, () => this.showMenu());
    this.registerCommand(context, COMMANDS.startCodex, () => this.runSaiTerminal("codex"));
    this.registerCommand(context, COMMANDS.startClaude, () => this.runSaiTerminal("claude"));
    this.registerCommand(context, COMMANDS.startOverlay, () => this.runSaiTerminal("overlay"));
    this.registerCommand(context, COMMANDS.wallet, () => this.showWallet());
    this.registerCommand(context, COMMANDS.refreshWallet, () => this.refreshWalletStatus());
    this.registerCommand(context, COMMANDS.openDashboard, () => this.runSaiTerminal("dashboard"));
    this.registerCommand(context, COMMANDS.installCli, () => this.confirmInstallCli());
    this.registerCommand(context, COMMANDS.diagnostics, () => this.showDiagnostics());

    void this.refreshWalletStatus();
    this.activationSetupDurationMs = elapsedDurationMs(startedAtMs, this.runtime.nowMs());
  }

  public async refreshWalletStatus(): Promise<WalletSnapshot | undefined> {
    if (!this.statusBarItem) {
      return undefined;
    }

    const seq = ++this.refreshSeq;
    try {
      const payload = await this.cli.readWalletJson();
      const snapshot = parseSaiWalletPayload(payload);
      if (seq !== this.refreshSeq) {
        // A newer refresh has already updated the status bar; do not clobber it.
        return snapshot;
      }
      this.setWalletStatus(formatWalletStatus(snapshot), walletTooltip(snapshot));
      this.maybeNotifyUpdate(snapshot);
      return snapshot;
    } catch (error) {
      if (seq !== this.refreshSeq) {
        return undefined;
      }
      this.applyWalletError(error);
      return undefined;
    }
  }

  private registerCommand(
    context: vscode.ExtensionContext,
    command: string,
    callback: CommandCallback
  ): void {
    context.subscriptions.push(this.api.commands.registerCommand(command, (...args: unknown[]) => {
      const startedAtMs = this.runtime.nowMs();
      try {
        const result = callback(...args);
        if (isPromiseLike(result)) {
          return Promise.resolve(result).finally(() => {
            this.recordCommandPerformance(command, startedAtMs);
          });
        }
        this.recordCommandPerformance(command, startedAtMs);
        return result;
      } catch (error) {
        this.recordCommandPerformance(command, startedAtMs);
        throw error;
      }
    }));
  }

  private async showMenu(): Promise<void> {
    const items: MenuItem[] = [
      { label: "Start Codex", action: "codex" },
      { label: "Start Claude", action: "claude" },
      { label: "Start Overlay", action: "overlay" },
      { label: "Wallet", action: "wallet" },
      { label: "Open Dashboard", action: "dashboard" },
      { label: "Install / Update CLI", action: "installCli" },
      { label: "Diagnostics", action: "diagnostics" }
    ];

    const selected = await this.api.window.showQuickPick(items, {
      title: "SAI",
      placeHolder: "Choose a SAI action"
    });

    if (!selected) {
      return;
    }

    await this.runMenuAction(selected.action);
  }

  private async runMenuAction(action: MenuAction): Promise<void> {
    switch (action) {
      case "codex":
        this.runSaiTerminal("codex");
        return;
      case "claude":
        this.runSaiTerminal("claude");
        return;
      case "overlay":
        this.runSaiTerminal("overlay");
        return;
      case "wallet":
        await this.showWallet();
        return;
      case "dashboard":
        this.runSaiTerminal("dashboard");
        return;
      case "installCli":
        await this.confirmInstallCli();
        return;
      case "diagnostics":
        await this.showDiagnostics();
        return;
    }
  }

  private async showDiagnostics(): Promise<void> {
    const diagnostics = saiPlacementDiagnosticsSnapshot();
    const performance = this.performanceSnapshot();
    let version = "unavailable";
    try {
      version = await (this.cli.readVersion ? this.cli.readVersion() : readSaiVersion(saiCliOptions()));
    } catch (error) {
      version = error instanceof SaiCliError ? error.reason : "unavailable";
    }

    const endpointAvailable = diagnostics.gatewayPlacementEndpointAvailable === undefined
      ? "unknown"
      : diagnostics.gatewayPlacementEndpointAvailable ? "yes" : "no";
    const fallbackUsed = diagnostics.fallbackToCliUsed ? "yes" : "no";
    await this.api.window.showQuickPick(
      [
        { label: `sai --version: ${version}` },
        { label: `Gateway placement endpoint: ${endpointAvailable}` },
        { label: `Placement CLI fallback used: ${fallbackUsed}` },
        { label: `Last gateway placement error: ${diagnostics.lastGatewayPlacementError ?? "none"}` },
        { label: `Last CLI placement error: ${diagnostics.lastCliPlacementError ?? "none"}` },
        { label: `Activation setup: ${formatDurationMs(performance.activationSetupDurationMs)}` },
        { label: `Measured SAI commands: ${performance.commandCount}` },
        { label: `Last command latency: ${formatCommandPerformance(performance.lastCommand)}` },
        { label: `Slowest command latency: ${formatCommandPerformance(
          performance.slowestCommand,
          performance.slowestCommand?.maxDurationMs
        )}` }
      ],
      {
        title: "SAI Diagnostics",
        placeHolder: "Recent local placement transport state"
      }
    );
  }

  private async showWallet(): Promise<void> {
    const snapshot = await this.refreshWalletStatus();
    if (!snapshot) {
      await this.api.window.showWarningMessage("SAI wallet is unavailable. Use SAI: Install CLI if the CLI is not installed.");
      return;
    }

    await this.api.window.showQuickPick(walletQuickPickItems(snapshot), {
      title: "SAI Wallet",
      placeHolder: "Balance, earned today, and recent wallet entries"
    });
  }

  // Passive nudge: the CLI auto-updates nowhere, so when the wallet read reports
  // a newer published version, surface it once (per version, per session) with a
  // one-click path into the existing Install / Update CLI flow. Never modal, and
  // tolerant of a host that lacks showInformationMessage (the unit-test fake).
  private maybeNotifyUpdate(snapshot: WalletSnapshot): void {
    const update = snapshot.update;
    if (!update?.available || !update.latest) {
      return;
    }
    if (this.notifiedUpdateVersion === update.latest) {
      return;
    }
    this.notifiedUpdateVersion = update.latest;
    const show = this.api.window.showInformationMessage;
    if (typeof show !== "function") {
      return;
    }
    const action = "Update CLI";
    const current = update.current ? ` (you have ${update.current})` : "";
    void Promise.resolve(
      show(`A new SAI CLI ${update.latest} is available${current}.`, action)
    ).then((choice) => {
      if (choice === action) {
        void this.confirmInstallCli();
      }
    });
  }

  private async confirmInstallCli(): Promise<void> {
    const confirmed = await this.api.window.showWarningMessage(
      "Install or update the SAI CLI globally with npm? This opens a visible integrated terminal and runs npm install -g @sponsoredai/cli. "
        + "The terminal starts in your home directory so a workspace .npmrc cannot redirect the install, but it still uses your user/global npm registry config.",
      { modal: true },
      "Open Terminal"
    );

    if (confirmed === "Open Terminal") {
      runSaiTerminalCommand(this.api.window, "installCli");
    }
  }

  public currentPlacementTool(): PlacementTool {
    return this.placementTool;
  }

  public performanceSnapshot(): SaiExtensionPerformanceSnapshot {
    let slowest: SaiCommandPerformance | undefined;
    let commandCount = 0;
    for (const value of this.commandPerformance.values()) {
      commandCount += value.count;
      if (!slowest || value.maxDurationMs > slowest.maxDurationMs) {
        slowest = value;
      }
    }
    return {
      activationSetupDurationMs: this.activationSetupDurationMs,
      commandCount,
      lastCommand: copyCommandPerformance(this.lastCommandPerformance),
      slowestCommand: copyCommandPerformance(slowest)
    };
  }

  private runSaiTerminal(action: "codex" | "claude" | "overlay" | "dashboard"): void {
    if (action === "codex" || action === "claude") {
      this.placementTool = action;
    }
    runSaiTerminalCommand(this.api.window, action, this.terminalOptions());
  }

  // Sets the wallet text/tooltip, remembering it so it can be restored after a
  // sponsor occupies the status bar. While a sponsor is shown, the wallet state
  // is recorded but not painted, so the ad is never clobbered by a refresh.
  private setWalletStatus(text: string, tooltip: string): void {
    this.lastWalletText = text;
    this.lastWalletTooltip = tooltip;
    if (this.statusBarItem && !this.sponsorActive) {
      this.statusBarItem.text = text;
      this.statusBarItem.tooltip = tooltip;
    }
  }

  // Show a mini-sponsor in the status bar during an attended wait. Clicking it
  // runs the supplied command (opening the sponsor's verified redirect).
  public showSponsorStatus(text: string, tooltip: string, command: string): void {
    if (!this.statusBarItem) {
      return;
    }
    this.sponsorActive = true;
    this.statusBarItem.text = text;
    this.statusBarItem.tooltip = tooltip;
    this.statusBarItem.command = command;
  }

  // Restore the wallet state when the wait ends.
  public clearSponsorStatus(): void {
    if (!this.statusBarItem) {
      return;
    }
    this.sponsorActive = false;
    this.statusBarItem.command = COMMANDS.showMenu;
    if (this.lastWalletText !== undefined) {
      this.statusBarItem.text = this.lastWalletText;
      this.statusBarItem.tooltip = this.lastWalletTooltip;
    }
  }

  private applyWalletError(error: unknown): void {
    if (!this.statusBarItem) {
      return;
    }

    if (error instanceof SaiCliError && error.reason === "notFound") {
      // State (CLI not found) is kept distinct from the action (install it),
      // so an empty balance is never confused with a missing CLI.
      this.setWalletStatus(
        "SAI: CLI not found",
        "SAI CLI was not found. Click to open SAI, then choose Install / Update CLI."
      );
      return;
    }

    if (error instanceof SaiCliError && error.reason === "timeout") {
      this.setWalletStatus("SAI: wallet unavailable", "SAI wallet read timed out. Try refreshing.");
      return;
    }

    if (error instanceof SaiCliError) {
      // Covers "failed" and "invalidJson". Never surface error.stderr here - it
      // can contain local file paths or other machine detail.
      this.setWalletStatus(
        "SAI: wallet unavailable",
        "SAI wallet could not be read. Check the SAI CLI installation and try refreshing."
      );
      return;
    }

    if (error instanceof WalletParseError) {
      this.setWalletStatus(
        "SAI: wallet unreadable",
        "SAI wallet returned an unexpected shape. Update the SAI CLI and try refreshing."
      );
      return;
    }

    // Unknown error: keep the tooltip generic so nothing sensitive leaks.
    this.setWalletStatus("SAI: refresh failed", "SAI wallet refresh failed. Try refreshing.");
  }

  private recordCommandPerformance(command: string, startedAtMs: number): void {
    const completedAtMs = this.runtime.nowMs();
    const durationMs = elapsedDurationMs(startedAtMs, completedAtMs);
    const previous = this.commandPerformance.get(command);
    const next = {
      command,
      count: (previous?.count ?? 0) + 1,
      lastDurationMs: durationMs,
      maxDurationMs: Math.max(previous?.maxDurationMs ?? 0, durationMs),
      lastCompletedAtMs: completedAtMs
    };
    this.commandPerformance.set(command, next);
    this.lastCommandPerformance = next;
  }
}

export function createExtensionController(
  api: VscodeApi = vscode,
  cli: SaiCliReader = DEFAULT_CLI,
  terminalOptions: SaiTerminalOptionsProvider = () => ({}),
  runtime: SaiExtensionRuntime = DEFAULT_EXTENSION_RUNTIME
): SaiExtensionController {
  return new SaiExtensionController(api, cli, terminalOptions, runtime);
}

// Wire the sidebar sponsor banner: a webview that shows a placement while the
// agent is waiting on the model (detected via the gateway's /v1/status), holds
// it long enough to qualify, reports the billable event, and refreshes the
// wallet. The production path uses the real VS Code APIs and local SAI
// transports; tests inject a fake clock, poller, and API so the activation-level
// banner journey is covered without launching an Extension Development Host.
export function setupAdBanner(
  context: vscode.ExtensionContext,
  controller: SaiExtensionController,
  api: AdBannerVscodeApi = vscode,
  runtime: AdBannerRuntime = DEFAULT_AD_BANNER_RUNTIME
): void {
  const provider = new SaiAdViewProvider([OPEN_SPONSOR_COMMAND]);
  context.subscriptions.push(
    api.window.registerWebviewViewProvider(SaiAdViewProvider.viewType, provider)
  );

  let lastInputMs = 0;
  context.subscriptions.push(
    // Returning focus to VS Code is a user presence signal for the local wait
    // surface. Qualification still requires the view to remain visible for the
    // backend's five-second hold.
    api.window.onDidChangeWindowState((state) => {
      if (state.focused) {
        lastInputMs = runtime.nowMs();
      }
    }),
    // Only genuine keyboard/mouse cursor activity counts as presence. Programmatic
    // selection changes (kind Command/undefined) are ignored, so a cloned repo's
    // extension cannot fire synthetic events to fake attendance, and an agent
    // editing files while you are away never keeps the wait "attended". Document
    // edits are deliberately NOT a presence signal for the same reason.
    api.window.onDidChangeTextEditorSelection((event) => {
      if (
        event.kind === api.TextEditorSelectionChangeKind.Keyboard
        || event.kind === api.TextEditorSelectionChangeKind.Mouse
      ) {
        lastInputMs = runtime.nowMs();
      }
    })
  );

  const engine = new AdEngine({
    fetchPlacement: async (attended) => parsePlacement(await runtime.fetchPlacement(
      { tool: controller.currentPlacementTool(), attended },
      runtime.placementOptions()
    )),
    recordQualified: async (placement, visibleSeconds, attended) => {
      await runtime.recordPlacementEvent(
        placement,
        { event: "qualified_5s", visibleSeconds, attended },
        runtime.placementOptions()
      );
    },
    showCard: (placement) => provider.showCard(placement),
    clearCard: () => provider.clearCard(),
    reveal: () => provider.reveal(),
    onQualified: () => {
      void controller.refreshWalletStatus();
    },
    showStatus: (placement) =>
      controller.showSponsorStatus(
        `$(megaphone) ${plainStatusText(placement.sponsor)}`,
        `${placement.message}\nSponsored - click to visit the sponsor.`,
        OPEN_SPONSOR_COMMAND
      ),
    clearStatus: () => controller.clearSponsorStatus()
  });

  context.subscriptions.push(
    api.commands.registerCommand(OPEN_SPONSOR_COMMAND, async () => {
      const target = safeHttpsUrl(provider.current?.click_url);
      if (!target) {
        return;
      }
      // The /c/ redirect records the paid click server-side, then forwards to
      // the sponsor. Only ever open the backend-issued https redirect.
      await api.env.openExternal(api.Uri.parse(target));
    })
  );

  const focused = (): boolean => api.window.state.focused;
  let ticking = false;
  const tick = async (): Promise<void> => {
    if (ticking || !focused()) {
      return;
    }
    ticking = true;
    try {
      const status = await runtime.readGatewayStatus(runtime.gatewayStatusOptions());
      if (!status) {
        return;
      }
      const attended = focused() && runtime.nowMs() - lastInputMs < ATTENDED_INPUT_WINDOW_MS;
      await engine.update({
        activeRequests: status.activeRequests,
        attended,
        viewVisible: provider.isVisible(),
        nowMs: runtime.nowMs()
      });
    } catch {
      // A poll failure (gateway down, transient CLI error) is non-fatal.
    } finally {
      ticking = false;
    }
  };

  const timer = runtime.setInterval(() => {
    void tick();
  }, AD_POLL_INTERVAL_MS);
  context.subscriptions.push({ dispose: () => runtime.clearInterval(timer) });
}

export function activate(context: vscode.ExtensionContext): SaiExtensionController {
  // Honor sai.cliPath for the wallet read too, so development against the source
  // CLI (or a non-standard install) drives the whole surface, not just the ads.
  const controller = createExtensionController(vscode, {
    readWalletJson: () => readSaiWalletJson(saiCliOptions()),
    readVersion: () => readSaiVersion(saiCliOptions())
  }, () => {
    const options = saiCliOptions();
    return {
      ...(options.command ? { saiCommand: options.command } : {}),
      launchCwd: terminalLaunchCwd(),
      platform: process.platform,
      sendDelayMs: terminalSendDelayMs()
    };
  });
  controller.activate(context);
  try {
    setupAdBanner(context, controller);
  } catch {
    // The launcher + wallet surface must come up even if the ad banner cannot.
  }
  return controller;
}

export function deactivate(): void {
  // VS Code disposes command, status bar, webview, and poller subscriptions
  // registered during activate.
}
