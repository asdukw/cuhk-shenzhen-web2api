import http from "node:http";
import repl from "node:repl";
import { lookup } from "node:dns/promises";
import { PassThrough } from "node:stream";
import { chromium } from "playwright-core";

const port = Number.parseInt(process.env.PORT || "3001", 10);
const steelApiUrl = (process.env.STEEL_API_URL || "http://steel:3000").replace(/\/$/, "");
const steelCdpHost = process.env.STEEL_CDP_HOST || "steel";
const token = process.env.STEEL_EXECUTOR_TOKEN || "";
const maxBodyBytes = 100 * 1024 * 1024;

let state = null;
let evaluationTail = Promise.resolve();

function json(response, status, value) {
  const body = JSON.stringify(value);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

function authorised(request) {
  return token && request.headers.authorization === `Bearer ${token}`;
}

async function readJson(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBodyBytes) {
      throw new Error("request body exceeds 100 MiB");
    }
    chunks.push(chunk);
  }
  if (chunks.length === 0) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

async function steelRequest(path, options = {}) {
  const response = await fetch(`${steelApiUrl}${path}`, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers || {}) },
  });
  const text = await response.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = { message: text };
  }
  if (!response.ok) {
    const error = new Error(
      `Steel HTTP ${response.status}: ${payload.message || text}`,
    );
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function internalWebSocketUrl(value) {
  const url = new URL(value);
  if (["localhost", "127.0.0.1", "0.0.0.0"].includes(url.hostname)) {
    // Chromium rejects remote-debugging WebSocket requests whose Host header
    // is neither localhost nor an IP address. Resolve Docker's service name
    // before connecting so the proxied Host header satisfies that check.
    const resolved = await lookup(steelCdpHost);
    url.hostname = resolved.address;
  }
  return url.toString();
}

function makeRepl(browser, context, page) {
  const input = new PassThrough();
  const output = new PassThrough();
  const server = repl.start({ prompt: "", input, output, terminal: false });
  server.context.browser = browser;
  server.context.context = context;
  server.context.page = page;
  return server;
}

async function disconnectCurrent() {
  if (!state) return;
  state.replServer.close();
  await state.browser.close().catch(() => {});
  state = null;
}

async function connectSession(details) {
  await disconnectCurrent();
  const browser = await chromium.connectOverCDP(
    await internalWebSocketUrl(details.websocketUrl),
  );
  const contexts = browser.contexts();
  const context = contexts[0] || (await browser.newContext());
  const pages = context.pages();
  const page = pages[0] || (await context.newPage());
  const replServer = makeRepl(browser, context, page);
  const connectedState = {
    sessionId: details.id,
    browser,
    context,
    page,
    replServer,
    details,
  };
  state = connectedState;
  browser.on("disconnected", () => {
    if (state?.browser === browser) state = null;
  });
  return connectedState;
}

async function findLiveSession(sessionId) {
  if (sessionId) {
    try {
      const details = await steelRequest(
        `/v1/sessions/${encodeURIComponent(sessionId)}`,
      );
      if (details.status === "live") return details;
    } catch (error) {
      // Browser data outlives individual session records. A saved ID can be
      // gone after a stack replacement; create or attach to a live session
      // against the same persistent profile in that case.
      if (error?.status !== 404) throw error;
    }
  }
  const result = await steelRequest("/v1/sessions");
  return (result.sessions || []).find((item) => item.status === "live") || null;
}

async function openSession(sessionId) {
  if ((sessionId && state?.sessionId === sessionId) || (!sessionId && state)) {
    return state;
  }
  const live = await findLiveSession(sessionId);
  if (live) return connectSession(live);
  const details = await steelRequest("/v1/sessions", {
    method: "POST",
    body: JSON.stringify({
      ...(sessionId ? { sessionId } : {}),
      persist: true,
      dimensions: { width: 1440, height: 1000 },
    }),
  });
  return connectSession(details);
}

function evaluate(code) {
  return new Promise((resolve, reject) => {
    state.replServer.eval(
      code,
      state.replServer.context,
      "steel-executor",
      (error, value) => (error ? reject(error) : resolve(value)),
    );
  });
}

async function executeSerial(code, timeoutSeconds) {
  const run = async () => {
    if (!state) throw new Error("no browser session is open");
    const timeout = Math.max(1, Number(timeoutSeconds) || 120) * 1000;
    let timer;
    try {
      return await Promise.race([
        evaluate(code),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("execution timed out")), timeout);
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  };
  const pending = evaluationTail.then(run, run);
  evaluationTail = pending.catch(() => {});
  return pending;
}

const server = http.createServer(async (request, response) => {
  try {
    if (request.method === "GET" && request.url === "/health") {
      return json(response, 200, { status: "ok", sessionId: state?.sessionId || null });
    }
    if (request.method !== "POST") {
      return json(response, 404, { error: "not found" });
    }
    if (!authorised(request)) {
      return json(response, 401, { error: "unauthorised" });
    }
    const body = await readJson(request);
    if (request.url === "/session/open") {
      const opened = await openSession(body.sessionId);
      return json(response, 200, {
        sessionId: opened.sessionId,
        debugUrl: opened.details.debugUrl,
        sessionViewerUrl: opened.details.sessionViewerUrl,
      });
    }
    if (request.url === "/execute") {
      if (!state || (body.sessionId && body.sessionId !== state.sessionId)) {
        await openSession(body.sessionId);
      }
      if (typeof body.code !== "string") {
        return json(response, 400, { error: "code must be a string" });
      }
      try {
        const value = await executeSerial(body.code, body.timeout);
        return json(response, 200, {
          result: value === undefined || value === null ? "" : String(value),
        });
      } catch (error) {
        return json(response, 200, { error: error?.message || String(error) });
      }
    }
    if (request.url === "/session/release") {
      if (!state || (body.sessionId && body.sessionId !== state.sessionId)) {
        return json(response, 200, { released: false });
      }
      await steelRequest(`/v1/sessions/${encodeURIComponent(state.sessionId)}/release`, {
        method: "POST",
        body: "{}",
      });
      await disconnectCurrent();
      return json(response, 200, { released: true });
    }
    return json(response, 404, { error: "not found" });
  } catch (error) {
    console.error(error);
    return json(response, 500, { error: error?.message || String(error) });
  }
});

server.listen(port, "0.0.0.0", () => {
  process.stdout.write(`Steel executor listening on ${port}\n`);
});
