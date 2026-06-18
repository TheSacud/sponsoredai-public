import * as vscode from "vscode";

// A sponsor placement as returned by `sai placement next --json` (the inner
// "placement" object). Only the fields the banner renders / echoes back.
export interface SponsorPlacement {
  readonly placement_id: string;
  readonly signature: string;
  readonly campaign_id?: string;
  readonly sponsor: string;
  readonly message: string;
  readonly url: string;
  readonly brand_icon_url?: string;
  readonly click_url?: string;
  readonly credit_amount?: number;
  readonly surface?: string;
  readonly tool?: string;
  readonly session_id?: string;
}

// The ad is only billable for a continuously-visible wait of at least this long
// (the backend is the authority; this is the client-side hold time before we
// report the qualifying event). A small margin over the backend's 5s bar.
export const MIN_VISIBLE_MS = 5200;
// Don't rotate to a fresh (billable) placement more often than this.
export const ROTATE_MS = 30_000;
// Floor between backend placement fetches, independent of rotation.
export const MIN_FETCH_GAP_MS = 8_000;

function optString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() !== "" ? value : undefined;
}

export function parsePlacement(payload: unknown): SponsorPlacement | undefined {
  if (!payload || typeof payload !== "object") {
    return undefined;
  }
  const inner = (payload as { placement?: unknown }).placement;
  const record = inner && typeof inner === "object" ? (inner as Record<string, unknown>) : undefined;
  if (!record) {
    return undefined;
  }
  const placementId = optString(record.placement_id);
  const signature = optString(record.signature);
  if (!placementId || !signature) {
    return undefined;
  }
  return {
    placement_id: placementId,
    signature,
    campaign_id: optString(record.campaign_id),
    sponsor: optString(record.sponsor) ?? "Sponsor",
    message: optString(record.message) ?? "",
    url: optString(record.url) ?? "",
    brand_icon_url: optString(record.brand_icon_url),
    click_url: optString(record.click_url),
    credit_amount: typeof record.credit_amount === "number" ? record.credit_amount : undefined,
    surface: optString(record.surface),
    tool: optString(record.tool),
    session_id: optString(record.session_id)
  };
}

export interface AdSignal {
  readonly activeRequests: number;
  readonly attended: boolean;
  readonly viewVisible: boolean;
  readonly nowMs: number;
}

export interface AdEngineDeps {
  // Fetch a placement for the wait. attended reflects focus + recent input.
  fetchPlacement(attended: boolean): Promise<SponsorPlacement | undefined>;
  // Report the qualifying (billable) event for a held, attended placement.
  recordQualified(placement: SponsorPlacement, visibleSeconds: number, attended: boolean): Promise<void>;
  showCard(placement: SponsorPlacement): void;
  clearCard?(): void;
  reveal(): void;
  onQualified(): void;
  // Optional second surface: a mini-sponsor shown elsewhere (e.g. the status
  // bar) only while the wait is in flight, cleared when it ends.
  showStatus?(placement: SponsorPlacement): void;
  clearStatus?(): void;
  log?(message: string): void;
}

interface ActiveCard {
  readonly placement: SponsorPlacement;
  readonly shownAtMs: number;
  qualified: boolean;
}

// Framework-free state machine driven by a periodic signal. Shows a sponsor
// placement when the agent starts waiting (and the user is attending), reports
// the qualifying event once the card has been visibly held long enough, and
// rotates at most every ROTATE_MS. Re-entrancy is guarded so overlapping ticks
// never double-fetch or double-bill.
export class AdEngine {
  private current?: ActiveCard;
  private busy = false;
  private lastFetchMs = Number.NEGATIVE_INFINITY;
  private statusShown = false;
  private waiting = false;

  public constructor(private readonly deps: AdEngineDeps) {}

  public async update(signal: AdSignal): Promise<void> {
    if (!this.busy) {
      if (!(await this.maybeQualify(signal))) {
        await this.maybeFetch(signal);
      }
    }
    // The status-bar mini-sponsor tracks the wait itself: shown only while a
    // placement is in hand during an attended in-flight wait, cleared the moment
    // the wait ends or attention is lost. The sidebar card is cleared when the
    // wait ends so the view reflects the current agent state.
    this.syncStatus(signal);
  }

  private syncStatus(signal: AdSignal): void {
    const waitActive = signal.activeRequests > 0;
    if (waitActive) {
      this.waiting = true;
    }
    const want = waitActive && signal.attended && this.current !== undefined;
    if (want && !this.statusShown) {
      if (this.current) {
        this.deps.showStatus?.(this.current.placement);
      }
      this.statusShown = true;
    } else if (!want && this.statusShown) {
      this.deps.clearStatus?.();
      this.statusShown = false;
    }
    // A finished wait retires its placement, so the next wait fetches a fresh
    // one and an already-billed card is never re-shown or kept holding the
    // rotation window across waits.
    if (!waitActive && this.waiting) {
      this.waiting = false;
      this.lastFetchMs = Number.NEGATIVE_INFINITY;
      if (this.current) {
        this.current = undefined;
        this.deps.clearCard?.();
      }
    }
  }

  public get currentPlacement(): SponsorPlacement | undefined {
    return this.current?.placement;
  }

  private async maybeQualify(signal: AdSignal): Promise<boolean> {
    const card = this.current;
    if (!card || card.qualified) {
      return false;
    }
    if (!signal.attended || !signal.viewVisible) {
      return false;
    }
    if (signal.nowMs - card.shownAtMs < MIN_VISIBLE_MS) {
      return false;
    }
    this.busy = true;
    try {
      const visibleSeconds = (signal.nowMs - card.shownAtMs) / 1000;
      await this.deps.recordQualified(card.placement, visibleSeconds, signal.attended);
      card.qualified = true;
      this.deps.onQualified();
      this.deps.log?.(`qualified placement ${card.placement.placement_id}`);
    } finally {
      this.busy = false;
    }
    return true;
  }

  private async maybeFetch(signal: AdSignal): Promise<void> {
    if (signal.activeRequests <= 0 || !signal.attended) {
      return;
    }
    const holdingUnqualified = this.current && !this.current.qualified;
    if (holdingUnqualified) {
      return;
    }
    const rotatedRecently = this.current && signal.nowMs - this.current.shownAtMs < ROTATE_MS;
    if (rotatedRecently) {
      return;
    }
    if (signal.nowMs - this.lastFetchMs < MIN_FETCH_GAP_MS) {
      return;
    }
    this.busy = true;
    this.lastFetchMs = signal.nowMs;
    try {
      const placement = await this.deps.fetchPlacement(signal.attended);
      if (!placement) {
        return;
      }
      this.current = { placement, shownAtMs: signal.nowMs, qualified: false };
      this.deps.showCard(placement);
      this.deps.reveal();
      this.deps.log?.(`showing placement ${placement.placement_id}`);
    } finally {
      this.busy = false;
    }
  }
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function safeHttpsUrl(value: string | undefined): string | undefined {
  if (!value) {
    return undefined;
  }
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" ? value : undefined;
  } catch {
    return undefined;
  }
}

const SAI_ICON_HOSTS = new Set(["sponsoredai.dev", "www.sponsoredai.dev"]);

export function safeBrandIconUrl(value: string | undefined): string | undefined {
  if (!value) {
    return undefined;
  }
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "https:" || !SAI_ICON_HOSTS.has(parsed.hostname.toLowerCase())) {
      return undefined;
    }
    return parsed.pathname.startsWith("/c/icon/") ? value : undefined;
  } catch {
    return undefined;
  }
}

interface IconSource {
  readonly src: string;
  readonly cspSource: string;
}

function iconSource(placement: SponsorPlacement | undefined): IconSource | undefined {
  if (!placement) {
    return undefined;
  }
  const remote = safeBrandIconUrl(placement.brand_icon_url);
  if (remote) {
    return { src: remote, cspSource: new URL(remote).origin };
  }
  return undefined;
}

export function renderAdHtml(placement: SponsorPlacement | undefined): string {
  const icon = iconSource(placement);
  const imgCsp = icon ? ` img-src ${icon.cspSource};` : "";
  const head =
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
    + `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';${imgCsp}">`
    + "<style>"
    + "body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);background:transparent;margin:0;padding:12px;font-size:13px;}"
    + ".tag{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--vscode-descriptionForeground);}"
    + ".card{border:1px solid var(--vscode-panel-border);border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:8px;}"
    + ".row{display:flex;align-items:center;gap:8px;}"
    + ".brandRow{display:flex;align-items:center;gap:10px;min-width:0;}"
    + ".brandIcon{width:28px;height:28px;border-radius:6px;object-fit:contain;flex:0 0 auto;}"
    + ".brandText{min-width:0;display:flex;flex-direction:column;gap:2px;}"
    + ".sponsor{font-weight:600;}"
    + ".msg{color:var(--vscode-foreground);}"
    + ".credit{color:var(--vscode-charts-green,#3fb950);font-weight:600;}"
    + "a.cta{display:inline-block;color:var(--vscode-textLink-foreground);text-decoration:none;}"
    + "a.cta:hover{text-decoration:underline;}"
    + ".empty{color:var(--vscode-descriptionForeground);}"
    + "</style></head><body>";
  const foot = "</body></html>";

  if (!placement) {
    return head + "<div class=\"empty\">No sponsor right now. An ad appears here while Claude or Codex is thinking.</div>" + foot;
  }

  const credit =
    typeof placement.credit_amount === "number" && placement.credit_amount > 0
      ? `<div class="credit">+${placement.credit_amount.toFixed(3)} credits when this wait qualifies</div>`
      : "";
  // command: URI keeps the click inside the extension (records the paid click
  // and opens the verified redirect) instead of letting the webview navigate.
  const cta = placement.click_url
    ? "<a class=\"cta\" href=\"command:sai.openSponsor\">Visit sponsor &rarr;</a>"
    : "";
  const iconHtml = icon
    ? `<img class="brandIcon" src="${escapeHtml(icon.src)}" alt="">`
    : "";

  return (
    head
    + "<div class=\"card\">"
    + "<div class=\"brandRow\">"
    + iconHtml
    + "<div class=\"brandText\">"
    + "<div class=\"tag\">Sponsored</div>"
    + `<div class="sponsor">${escapeHtml(placement.sponsor)}</div>`
    + "</div>"
    + "</div>"
    + `<div class="msg">${escapeHtml(placement.message)}</div>`
    + credit
    + cta
    + "</div>"
    + foot
  );
}

export class SaiAdViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "sai.adBanner";
  private view?: vscode.WebviewView;
  private placement?: SponsorPlacement;

  public resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: false, enableCommandUris: true };
    view.webview.html = renderAdHtml(this.placement);
    view.onDidDispose(() => {
      if (this.view === view) {
        this.view = undefined;
      }
    });
  }

  public showCard(placement: SponsorPlacement): void {
    this.placement = placement;
    if (this.view) {
      this.view.webview.html = renderAdHtml(placement);
    }
  }

  public clearCard(): void {
    this.placement = undefined;
    if (this.view) {
      this.view.webview.html = renderAdHtml(undefined);
    }
  }

  public reveal(): void {
    // preserveFocus: never steal focus from the editor for an ad.
    this.view?.show?.(true);
  }

  public isVisible(): boolean {
    return this.view?.visible ?? false;
  }

  public get current(): SponsorPlacement | undefined {
    return this.placement;
  }
}
