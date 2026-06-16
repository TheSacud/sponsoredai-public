import assert from "node:assert/strict";
import type { ChildProcess, ExecFileOptions } from "node:child_process";
import * as http from "node:http";
import test from "node:test";
import {
  fetchSaiPlacement,
  readGatewayStatus,
  readSaiWalletJson,
  recordSaiPlacementEvent,
  SaiCliError,
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

test("fetchSaiPlacement builds the vscode_ai_wait args and parses JSON", async () => {
  const captured: Captured[] = [];
  const payload = { placement: { placement_id: "plc_1", signature: "sig_1", sponsor: "Acme" } };
  const result = await fetchSaiPlacement(
    { tool: "codex", attended: true },
    { ...POSIX_OPTS, execFileRunner: fakeRunner(captured, JSON.stringify(payload)) }
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
  assert.deepEqual(result, payload);
});

test("fetchSaiPlacement omits --attended when not attended", async () => {
  const captured: Captured[] = [];
  await fetchSaiPlacement(
    { tool: "claude", attended: false },
    { ...POSIX_OPTS, execFileRunner: fakeRunner(captured, "{}") }
  );
  assert.equal(captured[captured.length - 1].args.includes("--attended"), false);
});

test("recordSaiPlacementEvent passes the ticket on stdin and sets event/visible-seconds", async () => {
  const captured: Captured[] = [];
  const ticket = { placement_id: "plc_1", signature: "sig_1", campaign_id: "c1" };
  const result = await recordSaiPlacementEvent(
    ticket,
    { event: "qualified_5s", visibleSeconds: 6.2, attended: true },
    { ...POSIX_OPTS, execFileRunner: fakeRunner(captured, JSON.stringify({ billable: true })) }
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
