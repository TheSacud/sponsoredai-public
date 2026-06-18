import assert from "node:assert/strict";
import type { ChildProcess, ExecFileOptions } from "node:child_process";
import * as http from "node:http";
import test from "node:test";
import {
  DEFAULT_PLACEMENT_TOOL,
  fetchGatewayPlacement,
  fetchSaiPlacement,
  readGatewayStatus,
  readSaiWalletJson,
  recordGatewayPlacementEvent,
  recordSaiPlacementEvent,
  SaiCliError,
  SAI_PLACEMENT_TRANSPORT_FIELD,
  saiPlacementDiagnosticsSnapshot,
  type ExecFileRunner
} from "../src/saiCli";

type Captured = { file: string; args: string[]; input?: string };

function fakeRunner(captured: Captured[], stdout: string): ExecFileRunner {
  return (file, args, _options, callback): ChildProcess => {
    const entry: Captured = { file, args };
    captured.push(entry);
    const stdin = {
      on(): void {
        /* no-op */
      },
      end(data?: string): void {
        if (data !== undefined) {
          entry.input = data;
        }
      }
    };
    queueMicrotask(() => callback(null, stdout, ""));
    return { stdin } as unknown as ChildProcess;
  };
}

const POSIX_OPTS = {
  platform: "linux" as NodeJS.Platform,
  pathDirs: ["/usr/local/bin"],
  trustedDirs: ["/usr/local/bin"],
  fileExists: () => true,
  realPath: (candidate: string) => candidate
};

function listenLocal(server: http.Server): Promise<number> {
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve((server.address() as { port: number }).port));
  });
}

function closeServer(server: http.Server): Promise<void> {
  return new Promise((resolve) => server.close(() => resolve()));
}

function requestBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", () => resolve(body));
  });
}

test("fetchSaiPlacement builds the vscode_ai_wait args and parses JSON", async () => {
  const captured: Captured[] = [];
  const payload = { placement: { placement_id: "plc_1", signature: "sig_1", sponsor: "Acme" } };
  const result = await fetchSaiPlacement(
    { tool: "codex", attended: true },
    { ...POSIX_OPTS, gateway: { enabled: false }, execFileRunner: fakeRunner(captured, JSON.stringify(payload)) }
  );

  assert.deepEqual(captured[captured.length - 1].args, [
    "placement",
    "next",
    "--json",
    "--surface",
    "vscode_ai_wait",
    "--tool",
    "codex",
    "--attended"
  ]);
  assert.deepEqual(result, {
    placement: { ...payload.placement, [SAI_PLACEMENT_TRANSPORT_FIELD]: "cli" }
  });
});

test("fetchSaiPlacement omits --attended when not attended", async () => {
  const captured: Captured[] = [];
  await fetchSaiPlacement(
    { tool: "claude", attended: false },
    { ...POSIX_OPTS, gateway: { enabled: false }, execFileRunner: fakeRunner(captured, "{}") }
  );
  assert.equal(captured[captured.length - 1].args.includes("--attended"), false);
});

test("recordSaiPlacementEvent passes the ticket on stdin and sets event/visible-seconds", async () => {
  const captured: Captured[] = [];
  const ticket = { placement_id: "plc_1", signature: "sig_1", campaign_id: "c1" };
  const result = await recordSaiPlacementEvent(
    ticket,
    { event: "qualified_5s", visibleSeconds: 6.2, attended: true },
    { ...POSIX_OPTS, gateway: { enabled: false }, execFileRunner: fakeRunner(captured, JSON.stringify({ billable: true })) }
  );

  const call = captured[captured.length - 1];
  assert.deepEqual(call.args, [
    "placement",
    "event",
    "--json",
    "--event",
    "qualified_5s",
    "--visible-seconds",
    "6.2",
    "--attended"
  ]);
  assert.deepEqual(JSON.parse(call.input ?? "{}"), ticket);
  assert.deepEqual(result, { billable: true });
});

test("fetchGatewayPlacement posts the vscode wait payload to the local gateway", async () => {
  const received: Array<{ url?: string; body: unknown }> = [];
  const payload = { placement: { placement_id: "plc_1", signature: "sig_1", sponsor: "Acme" } };
  const server = http.createServer(async (req, res) => {
    received.push({ url: req.url, body: JSON.parse(await requestBody(req)) as unknown });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(payload));
  });
  const port = await listenLocal(server);
  try {
    const result = await fetchGatewayPlacement({ tool: "codex", attended: true }, { port });
    assert.deepEqual(result, payload);
    assert.deepEqual(received, [
      {
        url: "/v1/sai/placements/next",
        body: { surface: "vscode_ai_wait", tool: "codex", attended: true }
      }
    ]);
  } finally {
    await closeServer(server);
  }
});

test("recordGatewayPlacementEvent posts the ticket in the body", async () => {
  const received: Array<{ url?: string; body: unknown }> = [];
  const ticket = { placement_id: "plc_1", signature: "sig_1", campaign_id: "c1" };
  const server = http.createServer(async (req, res) => {
    received.push({ url: req.url, body: JSON.parse(await requestBody(req)) as unknown });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ billable: true }));
  });
  const port = await listenLocal(server);
  try {
    const result = await recordGatewayPlacementEvent(
      ticket,
      { event: "qualified_5s", visibleSeconds: 5.2, attended: true },
      { port }
    );
    assert.deepEqual(result, { billable: true });
    assert.deepEqual(received, [
      {
        url: "/v1/sai/placements/event",
        body: { ticket, event: "qualified_5s", visible_seconds: 5.2, attended: true }
      }
    ]);
  } finally {
    await closeServer(server);
  }
});

test("gateway placement helpers ignore non-loopback hosts", async () => {
  const seen: string[] = [];
  const server = http.createServer(async (req, res) => {
    seen.push(req.url ?? "");
    await requestBody(req);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(req.url?.endsWith("/event") ? { billable: true } : { placement: null }));
  });
  const port = await listenLocal(server);
  try {
    await fetchGatewayPlacement({ attended: true }, { host: "example.com", port });
    await recordGatewayPlacementEvent(
      { placement_id: "plc_1", signature: "sig_1" },
      { visibleSeconds: 5.2, attended: true },
      { host: "example.com", port }
    );
    assert.deepEqual(seen, ["/v1/sai/placements/next", "/v1/sai/placements/event"]);
  } finally {
    await closeServer(server);
  }
});

test("fetchSaiPlacement uses the gateway when available and does not spawn the CLI", async () => {
  const captured: Captured[] = [];
  const payload = { placement: { placement_id: "plc_1", signature: "sig_1", sponsor: "Acme" } };
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(payload));
  });
  const port = await listenLocal(server);
  try {
    const result = await fetchSaiPlacement(
      { attended: true },
      { ...POSIX_OPTS, gateway: { port }, execFileRunner: fakeRunner(captured, "{}") }
    );
    assert.deepEqual(result, {
      placement: { ...payload.placement, [SAI_PLACEMENT_TRANSPORT_FIELD]: "gateway" }
    });
    assert.deepEqual(captured, []);
    assert.deepEqual(saiPlacementDiagnosticsSnapshot(), {
      gatewayPlacementEndpointAvailable: true,
      fallbackToCliUsed: false,
      lastGatewayPlacementError: undefined,
      lastCliPlacementError: undefined
    });
  } finally {
    await closeServer(server);
  }
});

test("fetchSaiPlacement falls back to CLI when gateway endpoint returns 404", async () => {
  const captured: Captured[] = [];
  const cliPayload = { placement: { placement_id: "plc_cli", signature: "sig_cli", sponsor: "CLI" } };
  const server = http.createServer((_req, res) => {
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: "Not found" } }));
  });
  const port = await listenLocal(server);
  try {
    const result = await fetchSaiPlacement(
      { tool: "codex", attended: true },
      { ...POSIX_OPTS, gateway: { port }, execFileRunner: fakeRunner(captured, JSON.stringify(cliPayload)) }
    );
    assert.deepEqual(result, {
      placement: { ...cliPayload.placement, [SAI_PLACEMENT_TRANSPORT_FIELD]: "cli" }
    });
    assert.equal(captured.length, 1);
    assert.equal(saiPlacementDiagnosticsSnapshot().gatewayPlacementEndpointAvailable, false);
    assert.equal(saiPlacementDiagnosticsSnapshot().fallbackToCliUsed, true);
    assert.match(saiPlacementDiagnosticsSnapshot().lastGatewayPlacementError ?? "", /^notFound:404$/);
  } finally {
    await closeServer(server);
  }
});

test("fetchSaiPlacement falls back to CLI when gateway is offline", async () => {
  const captured: Captured[] = [];
  const cliPayload = { placement: { placement_id: "plc_cli", signature: "sig_cli", sponsor: "CLI" } };
  const server = http.createServer((_req, res) => {
    res.writeHead(200);
    res.end("{}");
  });
  const port = await listenLocal(server);
  await closeServer(server);

  const result = await fetchSaiPlacement(
    { attended: true },
    { ...POSIX_OPTS, gateway: { port, timeoutMs: 300 }, execFileRunner: fakeRunner(captured, JSON.stringify(cliPayload)) }
  );

  assert.deepEqual(result, {
    placement: { ...cliPayload.placement, [SAI_PLACEMENT_TRANSPORT_FIELD]: "cli" }
  });
  assert.equal(captured.length, 1);
  assert.equal(saiPlacementDiagnosticsSnapshot().fallbackToCliUsed, true);
});

test("fetchSaiPlacement falls back to CLI when gateway returns invalid JSON", async () => {
  const captured: Captured[] = [];
  const cliPayload = { placement: { placement_id: "plc_cli", signature: "sig_cli", sponsor: "CLI" } };
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end("not-json");
  });
  const port = await listenLocal(server);
  try {
    const result = await fetchSaiPlacement(
      { attended: true },
      { ...POSIX_OPTS, gateway: { port }, execFileRunner: fakeRunner(captured, JSON.stringify(cliPayload)) }
    );
    assert.deepEqual(result, {
      placement: { ...cliPayload.placement, [SAI_PLACEMENT_TRANSPORT_FIELD]: "cli" }
    });
    assert.equal(captured.length, 1);
    assert.equal(saiPlacementDiagnosticsSnapshot().lastGatewayPlacementError, "invalidJson");
  } finally {
    await closeServer(server);
  }
});

test("fetchSaiPlacement falls back to CLI when gateway response is too large", async () => {
  const captured: Captured[] = [];
  const cliPayload = { placement: { placement_id: "plc_cli", signature: "sig_cli", sponsor: "CLI" } };
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end("x".repeat(129 * 1024));
  });
  const port = await listenLocal(server);
  try {
    const result = await fetchSaiPlacement(
      { attended: true },
      { ...POSIX_OPTS, gateway: { port }, execFileRunner: fakeRunner(captured, JSON.stringify(cliPayload)) }
    );
    assert.deepEqual(result, {
      placement: { ...cliPayload.placement, [SAI_PLACEMENT_TRANSPORT_FIELD]: "cli" }
    });
    assert.equal(captured.length, 1);
    assert.equal(saiPlacementDiagnosticsSnapshot().lastGatewayPlacementError, "responseTooLarge");
  } finally {
    await closeServer(server);
  }
});

test("recordSaiPlacementEvent uses the gateway when available and does not spawn the CLI", async () => {
  const captured: Captured[] = [];
  const ticket = { placement_id: "plc_1", signature: "sig_1", campaign_id: "c1" };
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ billable: true }));
  });
  const port = await listenLocal(server);
  try {
    const result = await recordSaiPlacementEvent(
      ticket,
      { event: "qualified_5s", visibleSeconds: 5.2, attended: true },
      { ...POSIX_OPTS, gateway: { port }, execFileRunner: fakeRunner(captured, "{}") }
    );
    assert.deepEqual(result, { billable: true });
    assert.deepEqual(captured, []);
  } finally {
    await closeServer(server);
  }
});

test("recordSaiPlacementEvent keeps CLI provenance and does not switch back to gateway", async () => {
  const captured: Captured[] = [];
  const seen: string[] = [];
  const ticket = {
    placement_id: "plc_cli",
    signature: "sig_cli",
    campaign_id: "c1",
    [SAI_PLACEMENT_TRANSPORT_FIELD]: "cli"
  };
  const server = http.createServer(async (req, res) => {
    seen.push(req.url ?? "");
    await requestBody(req);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ billable: true, via: "gateway" }));
  });
  const port = await listenLocal(server);
  try {
    const result = await recordSaiPlacementEvent(
      ticket,
      { event: "qualified_5s", visibleSeconds: 5.2, attended: true },
      { ...POSIX_OPTS, gateway: { port }, execFileRunner: fakeRunner(captured, JSON.stringify({ billable: true, via: "cli" })) }
    );
    assert.deepEqual(result, { billable: true, via: "cli" });
    assert.equal(captured.length, 1);
    assert.deepEqual(seen, []);
  } finally {
    await closeServer(server);
  }
});

test("fetchSaiPlacement uses a configured command instead of a reachable gateway", async () => {
  const captured: Captured[] = [];
  const seen: string[] = [];
  const cliPayload = { placement: { placement_id: "plc_cli", signature: "sig_cli", sponsor: "CLI" } };
  const server = http.createServer(async (req, res) => {
    seen.push(req.url ?? "");
    await requestBody(req);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ placement: { placement_id: "plc_gateway", signature: "sig_gateway" } }));
  });
  const port = await listenLocal(server);
  try {
    const result = await fetchSaiPlacement(
      { attended: true },
      {
        ...POSIX_OPTS,
        command: "/opt/custom/sai",
        trustCommand: true,
        gateway: { port },
        execFileRunner: fakeRunner(captured, JSON.stringify(cliPayload))
      }
    );
    assert.deepEqual(result, {
      placement: { ...cliPayload.placement, [SAI_PLACEMENT_TRANSPORT_FIELD]: "cli" }
    });
    assert.equal(captured.length, 1);
    assert.deepEqual(seen, []);
  } finally {
    await closeServer(server);
  }
});

test("fetchSaiPlacement defaults the placement tool to codex", async () => {
  const captured: Captured[] = [];
  await fetchSaiPlacement(
    { attended: false },
    { ...POSIX_OPTS, gateway: { enabled: false }, execFileRunner: fakeRunner(captured, "{}") }
  );
  assert.equal(DEFAULT_PLACEMENT_TOOL, "codex");
  assert.deepEqual(captured[captured.length - 1].args.slice(0, 7), [
    "placement",
    "next",
    "--json",
    "--surface",
    "vscode_ai_wait",
    "--tool",
    "codex"
  ]);
});

test("readGatewayStatus reads the active_requests count", async () => {
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ status: "ok", active_requests: 3 }));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as { port: number }).port;
  try {
    const status = await readGatewayStatus({ port });
    assert.deepEqual(status, { activeRequests: 3 });
  } finally {
    server.close();
  }
});

test("readGatewayStatus ignores non-loopback gateway hosts", async () => {
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ status: "ok", active_requests: 2 }));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as { port: number }).port;
  try {
    const status = await readGatewayStatus({ host: "example.com", port });
    assert.deepEqual(status, { activeRequests: 2 });
  } finally {
    server.close();
  }
});

test("readGatewayStatus resolves undefined when the gateway is unreachable", async () => {
  // Port 1 is privileged/unused; the connection is refused fast.
  const status = await readGatewayStatus({ port: 1, timeoutMs: 300 });
  assert.equal(status, undefined);
});

test("a configured cliPath outside the allowlist runs when trustCommand is set", async () => {
  const captured: Captured[] = [];
  await readSaiWalletJson({
    platform: "linux",
    command: "/opt/custom/sai",
    trustCommand: true,
    trustedDirs: ["/usr/local/bin"],
    fileExists: () => true,
    realPath: (candidate) => candidate,
    execFileRunner: fakeRunner(captured, JSON.stringify({ balance: 1 }))
  });
  assert.equal(captured[captured.length - 1].file, "/opt/custom/sai");
  assert.deepEqual(captured[captured.length - 1].args, ["wallet", "--json"]);
});

test("a configured cliPath is still rejected without trustCommand", async () => {
  await assert.rejects(
    () =>
      readSaiWalletJson({
        platform: "linux",
        command: "/opt/custom/sai",
        trustedDirs: ["/usr/local/bin"],
        fileExists: () => true,
        realPath: (candidate) => candidate
      }),
    (error) => error instanceof SaiCliError && error.reason === "notFound"
  );
});
