import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";
import type * as AdBannerModule from "../src/adBanner";

function loadAdBanner(): typeof AdBannerModule {
  const moduleWithLoader = Module as unknown as {
    _load(request: string, parent: unknown, isMain: boolean): unknown;
  };
  const originalLoad = moduleWithLoader._load;
  moduleWithLoader._load = function patchedLoad(request: string, parent: unknown, isMain: boolean): unknown {
    if (request === "vscode") {
      return {};
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    return require("../src/adBanner") as typeof AdBannerModule;
  } finally {
    moduleWithLoader._load = originalLoad;
  }
}

const ad = loadAdBanner();

function placement(extra: Record<string, unknown> = {}) {
  return {
    placement: {
      placement_id: "plc_1",
      signature: "sig_1",
      campaign_id: "c1",
      sponsor: "Acme",
      message: "Ship faster",
      url: "https://acme.example/x",
      click_url: "https://sponsoredai.dev/c/plc_1/clt_1",
      credit_amount: 0.02,
      surface: "vscode_ai_wait",
      tool: "claude",
      session_id: "sess_1",
      ...extra
    }
  };
}

test("parsePlacement returns the card for a valid payload", () => {
  const card = ad.parsePlacement(placement());
  assert.ok(card);
  assert.equal(card?.placement_id, "plc_1");
  assert.equal(card?.signature, "sig_1");
  assert.equal(card?.sponsor, "Acme");
  assert.equal(card?.brand_icon_url, undefined);
});

test("parsePlacement preserves local transport metadata", () => {
  const card = ad.parsePlacement(placement({ sai_transport: "cli" }));
  assert.equal(card?.sai_transport, "cli");
});

test("parsePlacement rejects payloads missing placement_id or signature", () => {
  assert.equal(ad.parsePlacement({ placement: null }), undefined);
  assert.equal(ad.parsePlacement({ placement: { signature: "s" } }), undefined);
  assert.equal(ad.parsePlacement({ placement: { placement_id: "p" } }), undefined);
  assert.equal(ad.parsePlacement({ placement: { placement_id: "", signature: "" } }), undefined);
});

test("renderAdHtml escapes content and sets a strict CSP", () => {
  const card = ad.parsePlacement(placement({ message: "<script>alert(1)</script>", sponsor: "A&B" }));
  const html = ad.renderAdHtml(card);
  assert.match(html, /Content-Security-Policy/);
  assert.match(html, /default-src 'none'/);
  assert.match(html, /&lt;script&gt;/);
  assert.equal(html.includes("<script>alert(1)</script>"), false);
  assert.match(html, /A&amp;B/);
});

test("renderAdHtml only loads trusted brand icons and gates the CTA on click_url", () => {
  const remoteIcon = ad.parsePlacement(placement({ brand_icon_url: "https://cdn.example/i.png" }));
  const remoteIconHtml = ad.renderAdHtml(remoteIcon);
  assert.equal(remoteIconHtml.includes("<img"), false);
  assert.equal(remoteIconHtml.includes("img-src https:"), false);

  const trustedIcon = ad.parsePlacement(placement({ brand_icon_url: "https://sponsoredai.dev/c/icon/c1" }));
  const trustedIconHtml = ad.renderAdHtml(trustedIcon);
  assert.match(trustedIconHtml, /<img class="brandIcon"/);
  assert.match(trustedIconHtml, /src="https:\/\/sponsoredai\.dev\/c\/icon\/c1"/);
  assert.match(trustedIconHtml, /img-src https:\/\/sponsoredai\.dev/);

  const ownPlacementWithoutIcon = ad.parsePlacement(placement({
    sponsor: "Sponsored AI",
    url: "https://sponsoredai.dev/?sai_placement=plc_1"
  }));
  const ownHtml = ad.renderAdHtml(ownPlacementWithoutIcon);
  assert.equal(ownHtml.includes("<img"), false);

  const noClick = ad.parsePlacement(placement({ click_url: undefined }));
  assert.equal(ad.renderAdHtml(noClick).includes("command:sai.openSponsor"), false);

  assert.match(ad.renderAdHtml(undefined), /An ad appears here/);
});

test("SaiAdViewProvider drives a stable webview banner journey", () => {
  const allowedCommandUris = ["sai.openSponsor"];
  const provider = new ad.SaiAdViewProvider(allowedCommandUris);
  let disposeHandler: (() => void) | undefined;
  let revealPreserveFocus: boolean | undefined;
  const view = {
    visible: true,
    webview: {
      options: {},
      html: ""
    },
    onDidDispose(callback: () => void) {
      disposeHandler = callback;
      return { dispose: () => undefined };
    },
    show(preserveFocus?: boolean) {
      revealPreserveFocus = preserveFocus;
    }
  };

  provider.resolveWebviewView(view as never);

  assert.deepEqual(view.webview.options, { enableScripts: false, enableCommandUris: allowedCommandUris });
  assert.match(view.webview.html, /data-testid="sai-ad-empty"/);
  assert.equal(provider.isVisible(), true);

  const card = ad.parsePlacement(placement({
    brand_icon_url: "https://sponsoredai.dev/c/icon/c1"
  }));
  assert.ok(card);

  provider.showCard(card);
  assert.equal(provider.current, card);
  assert.match(view.webview.html, /href="command:sai\.openSponsor"/);
  for (const testId of [
    "sai-ad-card",
    "sai-ad-label",
    "sai-ad-sponsor",
    "sai-ad-message",
    "sai-ad-credit",
    "sai-ad-cta",
    "sai-ad-brand-icon"
  ]) {
    assert.match(view.webview.html, new RegExp(`data-testid="${testId}"`));
  }

  provider.reveal();
  assert.equal(revealPreserveFocus, true);

  provider.clearCard();
  assert.equal(provider.current, undefined);
  assert.match(view.webview.html, /data-testid="sai-ad-empty"/);

  provider.showCard(card);
  disposeHandler?.();
  assert.equal(provider.isVisible(), false);

  const recreated = {
    ...view,
    webview: { options: {}, html: "" }
  };
  provider.resolveWebviewView(recreated as never);
  assert.match(recreated.webview.html, /data-testid="sai-ad-card"/);
});

test("safeHttpsUrl only allows SAI https click redirects", () => {
  assert.equal(ad.safeHttpsUrl("https://sponsoredai.dev/c/plc_1/clt_1"), "https://sponsoredai.dev/c/plc_1/clt_1");
  assert.equal(ad.safeHttpsUrl("https://www.sponsoredai.dev/c/plc_1/clt_1"), "https://www.sponsoredai.dev/c/plc_1/clt_1");
  assert.equal(ad.safeHttpsUrl("https://sponsoredai.dev/not-click"), undefined);
  assert.equal(ad.safeHttpsUrl("https://evil.example/c/plc_1/clt_1"), undefined);
  assert.equal(ad.safeHttpsUrl("http://sponsoredai.dev/click"), undefined);
  assert.equal(ad.safeHttpsUrl("not a url"), undefined);
});

function makeEngine() {
  const calls = {
    fetch: 0,
    qualified: 0,
    shown: [] as string[],
    cleared: 0,
    onQualified: 0,
    statusShown: [] as string[],
    statusCleared: 0
  };
  const card = ad.parsePlacement(placement());
  const engine = new ad.AdEngine({
    fetchPlacement: async () => {
      calls.fetch += 1;
      return card;
    },
    recordQualified: async () => {
      calls.qualified += 1;
    },
    showCard: (placementArg) => {
      calls.shown.push(placementArg.placement_id);
    },
    clearCard: () => {
      calls.cleared += 1;
    },
    reveal: () => undefined,
    onQualified: () => {
      calls.onQualified += 1;
    },
    showStatus: (placementArg) => {
      calls.statusShown.push(placementArg.placement_id);
    },
    clearStatus: () => {
      calls.statusCleared += 1;
    }
  });
  return { engine, calls };
}

test("AdEngine does not fetch when idle or unattended", async () => {
  const { engine, calls } = makeEngine();
  await engine.update({ activeRequests: 0, attended: true, viewVisible: true, nowMs: 0 });
  await engine.update({ activeRequests: 2, attended: false, viewVisible: true, nowMs: 1000 });
  assert.equal(calls.fetch, 0);
});

test("AdEngine shows one card on a wait and qualifies it after the visible threshold", async () => {
  const { engine, calls } = makeEngine();
  // Wait starts, user attending -> fetch + show exactly once.
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: 1000 });
  assert.equal(calls.fetch, 1);
  assert.deepEqual(calls.shown, ["plc_1"]);

  // Still within the same wait, before the threshold -> no re-fetch, no qualify.
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: 3000 });
  assert.equal(calls.fetch, 1);
  assert.equal(calls.qualified, 0);

  // Past the visible threshold, attended + visible -> qualify once.
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: 1000 + ad.MIN_VISIBLE_MS + 1 });
  assert.equal(calls.qualified, 1);
  assert.equal(calls.onQualified, 1);

  // Does not double-qualify.
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: 1000 + ad.MIN_VISIBLE_MS + 2000 });
  assert.equal(calls.qualified, 1);
});

test("AdEngine does not qualify while hidden or unattended", async () => {
  const { engine, calls } = makeEngine();
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: 0 });
  assert.equal(calls.fetch, 1);
  // Threshold elapsed but the view is hidden -> no qualify.
  await engine.update({ activeRequests: 1, attended: true, viewVisible: false, nowMs: ad.MIN_VISIBLE_MS + 10 });
  assert.equal(calls.qualified, 0);
  // Threshold elapsed and visible but no longer attended -> still no qualify.
  await engine.update({ activeRequests: 1, attended: false, viewVisible: true, nowMs: ad.MIN_VISIBLE_MS + 20 });
  assert.equal(calls.qualified, 0);
});

test("AdEngine shows a status sponsor during the wait and clears it when the wait ends", async () => {
  const { engine, calls } = makeEngine();
  // Wait active + attended + card in hand -> status sponsor shown once.
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: 1000 });
  assert.deepEqual(calls.statusShown, ["plc_1"]);
  assert.equal(calls.statusCleared, 0);
  // Still in the wait -> not shown again.
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: 2000 });
  assert.equal(calls.statusShown.length, 1);
  // Wait ends -> status cleared.
  await engine.update({ activeRequests: 0, attended: true, viewVisible: true, nowMs: 3000 });
  assert.equal(calls.statusCleared, 1);
  assert.equal(calls.cleared, 1);
});

test("AdEngine clears the status sponsor when attention is lost mid-wait", async () => {
  const { engine, calls } = makeEngine();
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: 1000 });
  assert.equal(calls.statusShown.length, 1);
  await engine.update({ activeRequests: 1, attended: false, viewVisible: true, nowMs: 2000 });
  assert.equal(calls.statusCleared, 1);
});

test("AdEngine retires the placement when the wait ends so the next wait fetches fresh", async () => {
  const { engine, calls } = makeEngine();
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: 1000 });
  assert.equal(calls.fetch, 1);
  // Wait ends -> the placement is retired (not kept holding the rotation window).
  await engine.update({ activeRequests: 0, attended: true, viewVisible: true, nowMs: 2000 });
  assert.equal(calls.cleared, 1);
  // A second wait fetches a fresh placement immediately; the fetch-gap floor is
  // only meant to avoid hammering the backend during one active wait.
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: 2500 });
  assert.equal(calls.fetch, 2);
});

test("AdEngine rotates to a new placement only after the rotate interval", async () => {
  const { engine, calls } = makeEngine();
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: 0 });
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: ad.MIN_VISIBLE_MS + 1 });
  assert.equal(calls.qualified, 1);
  // Qualified but still inside the rotate window -> no new fetch.
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: ad.MIN_VISIBLE_MS + 2000 });
  assert.equal(calls.fetch, 1);
  // Past the rotate window -> a fresh placement is fetched.
  await engine.update({ activeRequests: 1, attended: true, viewVisible: true, nowMs: ad.ROTATE_MS + ad.MIN_FETCH_GAP_MS + 10 });
  assert.equal(calls.fetch, 2);
});
