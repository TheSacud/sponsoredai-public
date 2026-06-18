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
  type GatewayStatusOptions,
  type PlacementTransportOptions,
  type ReadSaiOptions
} from "./saiCli";
import { runSaiTerminalCommand, type SaiTerminalCommandOptions } from "./terminals";
import { AdEngine, parsePlacement, safeHttpsUrl, SaiAdViewProvider } from "./adBanner";
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

export interface SaiCliReader {
  readWalletJson(): Promise<unknown>;
}

type MenuAction = "codex" | "claude" | "overlay" | "wallet" | "dashboard" | "installCli" | "diagnostics";

interface MenuItem extends vscode.QuickPickItem {
  readonly action: MenuAction;
}

const DEFAULT_CLI: SaiCliReader = {
  readWalletJson: readSaiWalletJson
};

type SaiTerminalOptionsProvider = () => SaiTerminalCommandOptions;
type PlacementTool = "codex" | "claude";

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

  public constructor(
    private readonly api: VscodeApi,
    private readonly cli: SaiCliReader = DEFAULT_CLI,
    private readonly terminalOptions: SaiTerminalOptionsProvider = () => ({})
  ) {}

  public activate(context: vscode.ExtensionContext): void {
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
    callback: (...args: unknown[]) => unknown
  ): void {
    context.subscriptions.push(this.api.commands.registerCommand(command, callback));
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
    let version = "unavailable";
    try {
      version = await readSaiVersion(saiCliOptions());
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
        { label: `Last CLI placement error: ${diagnostics.lastCliPlacementError ?? "none"}` }
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
}

export function createExtensionController(
  api: VscodeApi = vscode,
  cli: SaiCliReader = DEFAULT_CLI,
  terminalOptions: SaiTerminalOptionsProvider = () => ({})
): SaiExtensionController {
  return new SaiExtensionController(api, cli, terminalOptions);
}

// Wire the sidebar sponsor banner: a webview that shows a placement while the
// agent is waiting on the model (detected via the gateway's /v1/status), holds
// it long enough to qualify, reports the billable event, and refreshes the
// wallet. Uses the real vscode APIs directly (the controller above is the
// unit-tested, dependency-injected core); kept defensive so a missing API or a
// failing poll never throws into activation.
function setupAdBanner(context: vscode.ExtensionContext, controller: SaiExtensionController): void {
  const provider = new SaiAdViewProvider();
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(SaiAdViewProvider.viewType, provider)
  );

  let lastInputMs = 0;
  context.subscriptions.push(
    // Returning focus to VS Code is a user presence signal for the local wait
    // surface. Qualification still requires the view to remain visible for the
    // backend's five-second hold.
    vscode.window.onDidChangeWindowState((state) => {
      if (state.focused) {
        lastInputMs = Date.now();
      }
    }),
    // Only genuine keyboard/mouse cursor activity counts as presence. Programmatic
    // selection changes (kind Command/undefined) are ignored, so a cloned repo's
    // extension cannot fire synthetic events to fake attendance, and an agent
    // editing files while you are away never keeps the wait "attended". Document
    // edits are deliberately NOT a presence signal for the same reason.
    vscode.window.onDidChangeTextEditorSelection((event) => {
      if (
        event.kind === vscode.TextEditorSelectionChangeKind.Keyboard
        || event.kind === vscode.TextEditorSelectionChangeKind.Mouse
      ) {
        lastInputMs = Date.now();
      }
    })
  );

  const engine = new AdEngine({
    fetchPlacement: async (attended) => parsePlacement(await fetchSaiPlacement({ tool: controller.currentPlacementTool(), attended }, saiPlacementOptions())),
    recordQualified: async (placement, visibleSeconds, attended) => {
      await recordSaiPlacementEvent(placement, { event: "qualified_5s", visibleSeconds, attended }, saiPlacementOptions());
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
    vscode.commands.registerCommand(OPEN_SPONSOR_COMMAND, async () => {
      const target = safeHttpsUrl(provider.current?.click_url);
      if (!target) {
        return;
      }
      // The /c/ redirect records the paid click server-side, then forwards to
      // the sponsor. Only ever open the backend-issued https redirect.
      await vscode.env.openExternal(vscode.Uri.parse(target));
    })
  );

  const focused = (): boolean => vscode.window.state.focused;
  let ticking = false;
  const tick = async (): Promise<void> => {
    if (ticking || !focused()) {
      return;
    }
    ticking = true;
    try {
      const status = await readGatewayStatus(gatewayStatusOptions());
      if (!status) {
        return;
      }
      const attended = focused() && Date.now() - lastInputMs < ATTENDED_INPUT_WINDOW_MS;
      await engine.update({
        activeRequests: status.activeRequests,
        attended,
        viewVisible: provider.isVisible(),
        nowMs: Date.now()
      });
    } catch {
      // A poll failure (gateway down, transient CLI error) is non-fatal.
    } finally {
      ticking = false;
    }
  };

  const timer = setInterval(() => {
    void tick();
  }, AD_POLL_INTERVAL_MS);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

export function activate(context: vscode.ExtensionContext): SaiExtensionController {
  // Honor sai.cliPath for the wallet read too, so development against the source
  // CLI (or a non-standard install) drives the whole surface, not just the ads.
  const controller = createExtensionController(vscode, {
    readWalletJson: () => readSaiWalletJson(saiCliOptions())
  }, () => {
    const options = saiCliOptions();
    return options.command ? { saiCommand: options.command } : {};
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
