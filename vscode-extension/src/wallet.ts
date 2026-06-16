export interface WalletLedgerEntry {
  readonly timestamp: string;
  readonly kind: string;
  readonly amount: number;
  readonly source: string;
}

// Authoritative balances the backend recognises. All figures are AI credits,
// matching the CLI (`sai wallet --json`). "spendable" is the only figure a
// developer can spend or cash out right now (available + settled); "pending"
// is still maturing and therefore not yet spendable.
export interface WalletBackendSummary {
  readonly spendable: number;
  readonly pending: number;
  readonly authoritative: number;
}

export interface WalletSnapshot {
  // Local display ledger balance. NOT authoritative: the CLI reconciles it to
  // the backend but it can drift, and it is denominated in AI credits.
  readonly localBalance: number;
  // True only when the CLI reports the local ledger as authoritative.
  readonly localAuthoritative: boolean;
  // True only when a backend sync ran this session. It does NOT by itself mean
  // localBalance equals the backend balance — use `backend` for the real figure.
  readonly backendConfirmed: boolean;
  // Present only when the backend confirmed a real credit summary this session.
  readonly backend?: WalletBackendSummary;
  // Credits earned today, computed from the recent ledger window (see below).
  readonly earnedToday: number;
  // True when "earned today" may be undercounted because the CLI only returns
  // the most recent ledger entries, so earlier earnings today fall outside it.
  readonly earnedTodayApproximate: boolean;
  readonly recentEntries: readonly WalletLedgerEntry[];
}

export interface WalletDisplayItem {
  readonly label: string;
  readonly description?: string;
  readonly detail?: string;
}

export class WalletParseError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "WalletParseError";
  }
}

// The CLI returns only the last 8 ledger entries (cli.py: `wallet.entries()[-8:]`).
// When we receive a full window, earlier "earn" entries from today may be missing,
// so any "earned today" figure derived from it must be treated as approximate.
const MAX_LEDGER_WINDOW = 8;

export function parseSaiWalletPayload(payload: unknown, now = new Date()): WalletSnapshot {
  if (!isRecord(payload)) {
    throw new WalletParseError("SAI wallet payload must be an object.");
  }

  const localBalance = readFiniteNumber(payload.balance);
  if (localBalance === undefined) {
    throw new WalletParseError("SAI wallet payload is missing a numeric balance.");
  }

  const rawEntries = Array.isArray(payload.recent_entries) ? payload.recent_entries : [];
  const recentEntries = rawEntries.map(coerceEntry).filter((entry): entry is WalletLedgerEntry => entry !== undefined);
  const earnedToday = recentEntries
    // Local calendar day on purpose: "today" means the developer's today, not
    // a UTC day. Entries with an unparseable timestamp are excluded, which is
    // one reason the figure is surfaced as approximate.
    .filter((entry) => entry.kind === "earn" && isSameLocalDay(entry.timestamp, now))
    .reduce((total, entry) => total + Math.max(entry.amount, 0), 0);

  return {
    localBalance,
    localAuthoritative: payload.local_wallet_authoritative === true,
    backendConfirmed: payload.backend_confirmed === true,
    backend: parseBackendSummary(payload.backend),
    earnedToday: roundCredits(earnedToday),
    earnedTodayApproximate: rawEntries.length >= MAX_LEDGER_WINDOW,
    recentEntries
  };
}

export function formatCredits(value: number): string {
  const sign = value < 0 ? "-" : "";
  // 3 decimals to match the CLI's credit precision and avoid hiding micro-credits.
  return `${sign}${Math.abs(value).toFixed(3)} credits`;
}

export function formatWalletStatus(snapshot: WalletSnapshot): string {
  if (snapshot.backend) {
    return `SAI: ${formatCredits(snapshot.backend.spendable)} eligible`;
  }
  if (snapshot.localAuthoritative) {
    return `SAI: ${formatCredits(snapshot.localBalance)}`;
  }
  return `SAI: ${formatCredits(snapshot.localBalance)} (unconfirmed)`;
}

export function walletTooltip(snapshot: WalletSnapshot): string {
  if (snapshot.backend) {
    return "SAI wallet synced with the backend. Eligible = available + settled credits you can spend or cash out now; pending credit is still maturing.";
  }
  if (snapshot.backendConfirmed) {
    return "SAI wallet synced, but the backend did not return a credit summary. Showing the local display balance only.";
  }
  return "SAI wallet was not confirmed with the backend this session. Showing the local display balance, which is not authoritative.";
}

export function walletQuickPickItems(snapshot: WalletSnapshot): WalletDisplayItem[] {
  const items: WalletDisplayItem[] = [];

  if (snapshot.backend) {
    items.push({
      label: `${formatCredits(snapshot.backend.spendable)} eligible`,
      description: "Backend confirmed — available + settled credits you can spend or cash out now"
    });
    if (snapshot.backend.pending > 0) {
      items.push({
        label: `${formatCredits(snapshot.backend.pending)} pending`,
        description: "Maturing — not yet spendable"
      });
    }
  }

  items.push({
    label: `${formatCredits(snapshot.localBalance)} local display balance`,
    description: snapshot.backend
      ? "Local ledger estimate, reconciled to the backend"
      : snapshot.localAuthoritative
        ? "Local ledger (reported authoritative)"
        : "Local ledger — backend not confirmed this session"
  });

  items.push({
    label: `${formatCredits(snapshot.earnedToday)} earned today`,
    description: snapshot.earnedTodayApproximate
      ? "Approximate — based on the most recent ledger entries (local day)"
      : "Based on recent ledger entries (local day)"
  });

  if (snapshot.recentEntries.length === 0) {
    items.push({
      label: "No recent wallet entries"
    });
    return items;
  }

  for (const entry of [...snapshot.recentEntries].reverse()) {
    items.push({
      label: `${entry.kind} ${formatSignedCredits(entry.amount)}`,
      description: entry.source,
      detail: entry.timestamp
    });
  }

  return items;
}

function parseBackendSummary(value: unknown): WalletBackendSummary | undefined {
  if (!isRecord(value)) {
    return undefined;
  }

  const available = readFiniteNumber(value.available_balance);
  const settled = readFiniteNumber(value.settled_balance);
  const pending = readFiniteNumber(value.pending_balance) ?? 0;

  const reconcile = isRecord(value.reconcile) ? value.reconcile : undefined;
  const reconciledSpendable = reconcile ? readFiniteNumber(reconcile.spendable_balance) : undefined;

  // Prefer the spendable figure the CLI already computed; otherwise derive it
  // exactly as the CLI does (available + settled). Without either, this is not a
  // real summary and we fall back to the local balance rather than invent one.
  const spendable = reconciledSpendable !== undefined
    ? reconciledSpendable
    : available !== undefined || settled !== undefined
      ? (available ?? 0) + (settled ?? 0)
      : undefined;

  if (spendable === undefined) {
    return undefined;
  }

  const reconciledAuthoritative = reconcile ? readFiniteNumber(reconcile.authoritative_balance) : undefined;
  const authoritative = reconciledAuthoritative !== undefined ? reconciledAuthoritative : spendable + pending;

  return {
    spendable: roundCredits(spendable),
    pending: roundCredits(pending),
    authoritative: roundCredits(authoritative)
  };
}

function coerceEntry(value: unknown): WalletLedgerEntry | undefined {
  if (!isRecord(value)) {
    return undefined;
  }

  const amount = readFiniteNumber(value.amount);
  if (amount === undefined) {
    return undefined;
  }

  return {
    timestamp: readString(value.timestamp) ?? "",
    kind: readString(value.kind) ?? "entry",
    amount,
    source: readString(value.source) ?? ""
  };
}

function readFiniteNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function readString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSameLocalDay(timestamp: string, now: Date): boolean {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return false;
  }

  return date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate();
}

function roundCredits(value: number): number {
  return Math.round(value * 1e6) / 1e6;
}

function formatSignedCredits(value: number): string {
  const sign = value >= 0 ? "+" : "-";
  return `${sign}${formatCredits(Math.abs(value))}`;
}
