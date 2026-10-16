import {
  canonicalJson,
  classifyRequest,
  configuredSecret,
  constantTimeEqual,
  destinationMap,
  expectedP39LinkHash,
  exactOriginList,
  exactSignature,
  hmac,
  hourBucket,
  isoNow,
  requireDestinationKey,
  requireOpaqueId,
  requireUtc,
  sha256,
  signedDeckUrl,
  signedLinkUrl,
  signedOpenUrl,
  signedUnsubscribeUrl,
  validateHttpsOrigin,
  validateActivationReceipt,
  verifyRouteSignature,
} from "./logic.mjs";
import { DASHBOARD_HTML } from "./dashboard.ts";

interface D1Result { success: boolean; meta?: { changes?: number }; results?: unknown[]; }
interface D1Statement { bind(...values: unknown[]): D1Statement; first<T>(): Promise<T | null>; all<T>(): Promise<{ results: T[] }>; run(): Promise<D1Result>; }
interface D1Database { prepare(query: string): D1Statement; batch(statements: D1Statement[]): Promise<D1Result[]>; }
interface Env {
  DB: D1Database;
  PUBLIC_ORIGIN: string;
  DESTINATION_ORIGINS: string;
  DESTINATION_MAP_JSON: string;
  TRACKING_SIGNING_KEY: string;
  INTERNAL_API_KEY: string;
  ACTIVATION_SIGNING_KEY: string;
  ACCESS_TEAM_DOMAIN: string;
  ACCESS_AUD: string;
  ACCESS_JWKS_JSON: string;
  // Staging-only fallback when the narrow Cloudflare OAuth grant cannot create
  // an Access application. This binding is deliberately optional: without it,
  // the dashboard remains Cloudflare-Access-only and fail closed.
  DASHBOARD_ACCESS_SECRET?: string;
  // Workers static-assets binding serving the pitch deck PDF. Deliberately
  // optional: without it, the deck route falls back to a destination-map
  // redirect (or 404) instead of failing the whole Worker.
  ASSETS?: { fetch(request: Request): Promise<Response> };
  DECK_ASSET_PATH?: string;
}

type Link = { link_id: string; campaign_id: string; action_id: string; destination_key: string; link_hash: string; expires_at: string; activated_at: string | null; receipt_hash: string | null; };
type CountRow = { count?: number; attempts?: number };

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };
const NO_STORE = { "cache-control": "no-store", "referrer-policy": "no-referrer" };
const DASHBOARD_COOKIE = "__Host-publik_dashboard";
const DASHBOARD_SESSION_SECONDS = 15 * 60;
const DASHBOARD_LOGIN_ATTEMPTS = 5;
const DASHBOARD_LOGIN_WINDOW_MS = 5 * 60 * 1000;

function json(payload: unknown, status = 200): Response { return new Response(JSON.stringify(payload), { status, headers: JSON_HEADERS }); }
function denied(): Response { return json({ error: "not_found" }, 404); }
function badRequest(): Response { return json({ error: "invalid_request" }, 400); }
function methodNotAllowed(): Response { return new Response(null, { status: 405, headers: { ...NO_STORE, allow: "GET, POST" } }); }

async function readJson(request: Request, maximum = 16384): Promise<unknown> {
  const length = Number(request.headers.get("content-length") || "0");
  if (!Number.isFinite(length) || length > maximum) throw new Error("body too large");
  const body = await request.text();
  if (body.length > maximum) throw new Error("body too large");
  return JSON.parse(body);
}

async function requireInternal(request: Request, env: Env): Promise<boolean> {
  const presented = request.headers.get("x-publik-internal-key") || "";
  return configuredSecret(env.INTERNAL_API_KEY) && configuredSecret(presented) && constantTimeEqual(presented, env.INTERNAL_API_KEY);
}

function origin(env: Env): string { return validateHttpsOrigin(env.PUBLIC_ORIGIN); }
function destinations(env: Env): Map<string, string> { return destinationMap(env.DESTINATION_MAP_JSON, exactOriginList(env.DESTINATION_ORIGINS)); }

async function getLink(db: D1Database, linkId: string): Promise<Link | null> {
  return db.prepare("SELECT link_id, campaign_id, action_id, destination_key, link_hash, expires_at, activated_at, receipt_hash FROM links WHERE link_id = ?").bind(linkId).first<Link>();
}

function activatedLink(link: Link): boolean { return Boolean(link.activated_at); }
function trackingWindowOpen(link: Link, now: string): boolean { return now <= link.expires_at; }

async function mintLink(request: Request, env: Env): Promise<Response> {
  if (!await requireInternal(request, env)) return denied();
  try {
    const body = await readJson(request);
    if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).sort().join(",") !== "action_id,campaign_id,destination_key,expires_at,link_hash,link_id") throw new Error("unexpected link fields");
    const link = body as Record<string, unknown>;
    requireOpaqueId(link.campaign_id, "campaign");
    requireOpaqueId(link.action_id, "action");
    requireOpaqueId(link.link_id, "link");
    requireDestinationKey(link.destination_key);
    if (typeof link.link_hash !== "string" || !/^sha256:[a-f0-9]{64}$/u.test(link.link_hash)) throw new Error("invalid P39 link hash");
    requireUtc(link.expires_at);
    if (link.expires_at <= isoNow()) throw new Error("expired link");
    if (!destinations(env).has(link.destination_key as string)) throw new Error("destination key is not approved");
    const p39Link = { campaign_id: link.campaign_id as string, action_id: link.action_id as string, link_id: link.link_id as string, destination_key: link.destination_key as string, expires_at: link.expires_at as string };
    if (!await constantTimeEqual(link.link_hash as string, await expectedP39LinkHash(p39Link, origin(env), env.TRACKING_SIGNING_KEY))) throw new Error("P39 link hash does not match link artifact");
    const now = isoNow();
    const result = await env.DB.prepare("INSERT INTO links (link_id, campaign_id, action_id, destination_key, link_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)")
      .bind(link.link_id, link.campaign_id, link.action_id, link.destination_key, link.link_hash, link.expires_at, now).run();
    if (!result.success) return json({ error: "link_conflict" }, 409);
    const publicLink = { link_id: link.link_id as string, campaign_id: link.campaign_id as string, action_id: link.action_id as string, destination_key: link.destination_key as string, expires_at: link.expires_at as string };
    return json({
      link_id: publicLink.link_id,
      action_id: publicLink.action_id,
      redirect_url: await signedLinkUrl(publicLink, origin(env), env.TRACKING_SIGNING_KEY),
      open_url: await signedOpenUrl(publicLink, origin(env), env.TRACKING_SIGNING_KEY),
      unsubscribe_url: await signedUnsubscribeUrl(publicLink, origin(env), env.TRACKING_SIGNING_KEY),
      deck_url: await signedDeckUrl(publicLink, origin(env), env.TRACKING_SIGNING_KEY),
      activation_required: true,
    }, 201);
  } catch {
    return badRequest();
  }
}

async function activateLink(request: Request, env: Env): Promise<Response> {
  if (!await requireInternal(request, env)) return denied();
  try {
    const body = await readJson(request);
    if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).sort().join(",") !== "link_id,receipt") throw new Error("unexpected activation fields");
    const { link_id: linkId, receipt } = body as { link_id: unknown; receipt: unknown };
    requireOpaqueId(linkId, "link");
    await validateActivationReceipt(receipt);
    const activationMac = request.headers.get("x-publik-activation-mac") || "";
    const link = await getLink(env.DB, linkId as string);
    if (!link || link.action_id !== (receipt as { action_id: string }).action_id || link.receipt_hash) return denied();
    if ((receipt as { executed_at: string }).executed_at > link.expires_at) return badRequest();
    const receiptValue = receipt as { executed_at: string; receipt_hash: string };
    const activationClaims = { accepted_at: receiptValue.executed_at, action_id: link.action_id, link_hash: link.link_hash, link_id: link.link_id, receipt_hash: receiptValue.receipt_hash, receipt_id: `receipt_${receiptValue.receipt_hash.slice("sha256:".length, "sha256:".length + 32)}` };
    if (!await constantTimeEqual(activationMac, await hmac(activationClaims, env.ACTIVATION_SIGNING_KEY))) throw new Error("activation MAC invalid");
    const updated = await env.DB.prepare("UPDATE links SET activated_at = ?, receipt_hash = ? WHERE link_id = ? AND activated_at IS NULL AND receipt_hash IS NULL")
      .bind((receipt as { executed_at: string }).executed_at, (receipt as { receipt_hash: string }).receipt_hash, link.link_id).run();
    if (!updated.success || updated.meta?.changes !== 1) return json({ error: "activation_conflict" }, 409);
    return json({ link_id: link.link_id, action_id: link.action_id, activated: true }, 201);
  } catch {
    return badRequest();
  }
}

async function recentCount(db: D1Database, actionId: string, occurredAt: string): Promise<number> {
  const windowStart = new Date(Date.parse(occurredAt) - 60000).toISOString().replace(/\.\d{3}Z$/u, "Z");
  const row = await db.prepare("SELECT COUNT(*) AS count FROM events WHERE action_id = ? AND occurred_at >= ?").bind(actionId, windowStart).first<CountRow>();
  return Number(row?.count || 0);
}

async function persistEvent(request: Request, env: Env, link: Link, now: string, type: "open" | "click" | "deck_view"): Promise<void> {
  const currentCount = await recentCount(env.DB, link.action_id, now);
  const classification = classifyRequest({ eventType: type, occurredAt: now, acceptedAt: link.activated_at as string, userAgent: request.headers.get("user-agent") || "", cf: (request as Request & { cf?: Record<string, unknown> }).cf || {}, recentCount: currentCount });
  const edgePseudonym = await hmac({ link_id: link.link_id, event_type: type, hour_bucket: hourBucket(now), edge_ip: request.headers.get("cf-connecting-ip") || "", user_agent: request.headers.get("user-agent") || "" }, env.TRACKING_SIGNING_KEY);
  const fingerprint = await sha256(edgePseudonym);
  const eventId = `event_${fingerprint.slice("sha256:".length, "sha256:".length + 32)}`;
  const inserted = await env.DB.prepare(
    "INSERT OR IGNORE INTO events (event_id, link_id, action_id, occurred_at, hour_bucket, event_type, source_kind, user_agent_family, network_class, machine_cadence, fanout_count, event_fingerprint_hash, classification, reason_codes_json, created_at) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE (SELECT COUNT(*) FROM events WHERE action_id = ? AND hour_bucket = ?) < 20"
  ).bind(eventId, link.link_id, link.action_id, now, hourBucket(now), type, classification.sourceKind, classification.userAgentFamily, classification.networkClass, classification.machineCadence ? 1 : 0, classification.fanoutCount, fingerprint, classification.classification, JSON.stringify(classification.reasonCodes), now, link.action_id, hourBucket(now)).run();
  if (!inserted.success) throw new Error("event persistence failed");
}

async function redirect(request: Request, env: Env, url: URL, linkId: string): Promise<Response> {
  try {
    requireOpaqueId(linkId, "link");
    const signature = exactSignature(url);
    const link = await getLink(env.DB, linkId);
    const now = isoNow();
    if (!link || !activatedLink(link) || !await verifyRouteSignature(link, origin(env), signature, "redirect", env.TRACKING_SIGNING_KEY)) return denied();
    const destination = destinations(env).get(link.destination_key);
    if (!destination) return denied();
    // The signed, allow-listed destination stays usable after the telemetry window.
    if (trackingWindowOpen(link, now)) {
      try { await persistEvent(request, env, link, now, "click"); } catch { /* Tracking must not make a valid owned link dead. */ }
    }
    return new Response(null, { status: 302, headers: { ...NO_STORE, location: destination } });
  } catch {
    return denied();
  }
}

async function openPixel(request: Request, env: Env, url: URL, linkId: string): Promise<Response> {
  if (request.method === "HEAD") return new Response(null, { status: 204, headers: NO_STORE });
  if (request.method !== "GET") return methodNotAllowed();
  try {
    requireOpaqueId(linkId, "link");
    const signature = exactSignature(url);
    const link = await getLink(env.DB, linkId);
    const now = isoNow();
    if (!link || !activatedLink(link) || !await verifyRouteSignature(link, origin(env), signature, "open", env.TRACKING_SIGNING_KEY)) return denied();
    if (trackingWindowOpen(link, now)) {
      try { await persistEvent(request, env, link, now, "open"); } catch { /* Pixel delivery remains safe when telemetry is unavailable. */ }
    }
    const pixel = Uint8Array.from(atob("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"), (character) => character.charCodeAt(0));
    return new Response(pixel, { headers: { ...NO_STORE, "content-type": "image/gif" } });
  } catch {
    return denied();
  }
}

async function unsubscribe(request: Request, env: Env, url: URL, linkId: string): Promise<Response> {
  if (request.method === "GET") return new Response(null, { status: 405, headers: { ...NO_STORE, allow: "POST" } });
  if (request.method !== "POST") return methodNotAllowed();
  try {
    requireOpaqueId(linkId, "link");
    const signature = exactSignature(url);
    const link = await getLink(env.DB, linkId);
    const now = isoNow();
    if (!link || !await verifyRouteSignature(link, origin(env), signature, "unsubscribe", env.TRACKING_SIGNING_KEY)) return denied();
    if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/x-www-form-urlencoded") return badRequest();
    const form = await request.formData();
    if ([...form.keys()].length !== 1 || form.get("List-Unsubscribe") !== "One-Click") return badRequest();
    const idempotency = await sha256(`${link.link_id}|${link.action_id}|${signature}`);
    const denyClaims = { action_id: link.action_id, link_id: link.link_id, received_at: now, suppression_scope: "permanent", delivery_denied: true, delivery_authorized: false, tracking_authorized: false, idempotency_fingerprint_hash: idempotency, duplicate_decision: "deny_already_recorded", send_gate_must_enforce: true };
    const denyMac = await hmac(denyClaims, env.TRACKING_SIGNING_KEY);
    let results: D1Result[];
    try {
      results = await env.DB.batch([
        env.DB.prepare("INSERT OR IGNORE INTO unsubscribe_denials (action_id, link_id, received_at, idempotency_fingerprint_hash, deny_mac, created_at) VALUES (?, ?, ?, ?, ?, ?)").bind(link.action_id, link.link_id, now, idempotency, denyMac, now),
        env.DB.prepare("INSERT OR IGNORE INTO suppression_outbox (action_id, denial_fingerprint_hash, deny_mac, created_at) VALUES (?, ?, ?, ?)").bind(link.action_id, idempotency, denyMac, now),
      ]);
    } catch {
      return json({ error: "unsubscribe_unavailable" }, 503);
    }
    if (results.length !== 2 || results.some((result) => !result.success)) return json({ error: "unsubscribe_unavailable" }, 503);
    return new Response(null, { status: 204, headers: NO_STORE });
  } catch {
    return badRequest();
  }
}

const DECK_HEADERS: Record<string, string> = {
  "content-type": "application/pdf",
  "content-disposition": 'inline; filename="Publik_Pitch_Deck.pdf"',
  "cache-control": "private, no-store",
  "x-robots-tag": "noindex",
  "referrer-policy": "no-referrer",
};

async function deckAsset(env: Env): Promise<Response | null> {
  if (!env.ASSETS) return null;
  try {
    const assetUrl = new URL(env.DECK_ASSET_PATH || "/deck.pdf", origin(env));
    const assetResponse = await env.ASSETS.fetch(new Request(assetUrl.toString()));
    return assetResponse.status === 200 ? assetResponse : null;
  } catch {
    return null;
  }
}

async function deckFallback(env: Env): Promise<Response> {
  const destination = destinations(env).get("deck");
  return destination ? new Response(null, { status: 302, headers: { ...NO_STORE, location: destination } }) : denied();
}

async function deckView(request: Request, env: Env, url: URL, linkId: string): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") return methodNotAllowed();
  try {
    requireOpaqueId(linkId, "link");
    const signature = exactSignature(url);
    const link = await getLink(env.DB, linkId);
    const now = isoNow();
    if (!link || !activatedLink(link) || !await verifyRouteSignature(link, origin(env), signature, "deck", env.TRACKING_SIGNING_KEY)) return denied();
    // The signed deck stays servable after its telemetry window closes, same
    // durability rule as /r/. Only a GET inside the window records a view.
    if (request.method === "GET" && trackingWindowOpen(link, now)) {
      try { await persistEvent(request, env, link, now, "deck_view"); } catch { /* Deck delivery remains safe when telemetry is unavailable. */ }
    }
    const asset = await deckAsset(env);
    if (!asset) return deckFallback(env);
    return new Response(request.method === "HEAD" ? null : asset.body, { status: 200, headers: DECK_HEADERS });
  } catch {
    return denied();
  }
}

function base64urlBytes(value: string): Uint8Array {
  const padded = value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - (value.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
}

function base64urlEncodeBytes(value: Uint8Array): string {
  return btoa(String.fromCharCode(...value)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

function base64urlText(value: string): string {
  return base64urlEncodeBytes(new TextEncoder().encode(value));
}

function base64urlTextDecode(value: string): string {
  return new TextDecoder().decode(base64urlBytes(value));
}

async function accessAllowed(request: Request, env: Env): Promise<boolean> {
  const token = request.headers.get("cf-access-jwt-assertion");
  if (!token) return false;
  const pieces = token.split(".");
  if (pieces.length !== 3) return false;
  try {
    if (pieces.some((piece) => !/^[A-Za-z0-9_-]+$/u.test(piece) || base64urlEncodeBytes(base64urlBytes(piece)) !== piece)) return false;
    const header = JSON.parse(new TextDecoder().decode(base64urlBytes(pieces[0]))) as { alg?: string; kid?: string };
    const payload = JSON.parse(new TextDecoder().decode(base64urlBytes(pieces[1]))) as { iss?: string; aud?: string[] | string; exp?: number };
    const expectedIssuer = `https://${env.ACCESS_TEAM_DOMAIN}`;
    if (header.alg !== "RS256" || typeof header.kid !== "string" || payload.iss !== expectedIssuer || !Array.isArray(payload.aud) || !payload.aud.includes(env.ACCESS_AUD) || typeof payload.exp !== "number" || payload.exp * 1000 <= Date.now()) return false;
    const jwks = JSON.parse(env.ACCESS_JWKS_JSON) as { keys?: JsonWebKey[] };
    const jwk = jwks.keys?.find((key) => key.kid === header.kid && key.kty === "RSA");
    if (!jwk) return false;
    const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    return crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, base64urlBytes(pieces[2]), new TextEncoder().encode(`${pieces[0]}.${pieces[1]}`));
  } catch { return false; }
}

function cookieValue(request: Request, name: string): string | null {
  const encodedName = `${name}=`;
  for (const part of (request.headers.get("cookie") || "").split(";")) {
    const value = part.trim();
    if (value.startsWith(encodedName)) return value.slice(encodedName.length);
  }
  return null;
}

function dashboardSecretConfigured(env: Env): boolean {
  return configuredSecret(env.DASHBOARD_ACCESS_SECRET || "");
}

function exactDashboardBearer(request: Request): string | null {
  const authorization = request.headers.get("authorization") || "";
  if (!authorization.startsWith("Bearer ")) return null;
  const value = authorization.slice("Bearer ".length);
  return value && !/\s/u.test(value) ? value : null;
}

async function dashboardBearerAllowed(request: Request, env: Env): Promise<boolean> {
  const presented = exactDashboardBearer(request);
  return Boolean(presented && dashboardSecretConfigured(env) && await constantTimeEqual(presented, env.DASHBOARD_ACCESS_SECRET as string));
}

type DashboardSession = { exp: number; iat: number; nonce: string; v: 1 };

function validDashboardSession(value: unknown, nowSeconds: number): value is DashboardSession {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return Object.keys(record).sort().join(",") === "exp,iat,nonce,v"
    && record.v === 1
    && Number.isInteger(record.iat)
    && Number.isInteger(record.exp)
    && typeof record.nonce === "string"
    && /^[a-f0-9]{32}$/u.test(record.nonce)
    && record.iat <= nowSeconds + 60
    && record.exp > nowSeconds
    && record.exp - record.iat === DASHBOARD_SESSION_SECONDS;
}

async function mintDashboardSession(env: Env): Promise<string> {
  if (!dashboardSecretConfigured(env)) throw new Error("dashboard fallback is not configured");
  const iat = Math.floor(Date.now() / 1000);
  const session: DashboardSession = {
    v: 1,
    iat,
    exp: iat + DASHBOARD_SESSION_SECONDS,
    nonce: Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join(""),
  };
  const payload = base64urlText(canonicalJson(session));
  return `${payload}.${await hmac({ purpose: "publik_dashboard_session", payload }, env.DASHBOARD_ACCESS_SECRET as string)}`;
}

async function dashboardSessionAllowed(request: Request, env: Env): Promise<boolean> {
  if (!dashboardSecretConfigured(env)) return false;
  const token = cookieValue(request, DASHBOARD_COOKIE);
  if (!token) return false;
  const parts = token.split(".");
  if (parts.length !== 2 || !/^[A-Za-z0-9_-]+$/u.test(parts[0]) || !/^[A-Za-z0-9_-]{43}$/u.test(parts[1])) return false;
  try {
    const payload = JSON.parse(base64urlTextDecode(parts[0]));
    if (!validDashboardSession(payload, Math.floor(Date.now() / 1000))) return false;
    const expected = await hmac({ purpose: "publik_dashboard_session", payload: parts[0] }, env.DASHBOARD_ACCESS_SECRET as string);
    return constantTimeEqual(parts[1], expected);
  } catch { return false; }
}

function dashboardCookie(session: string): string {
  return `${DASHBOARD_COOKIE}=${session}; Path=/; Max-Age=${DASHBOARD_SESSION_SECONDS}; HttpOnly; Secure; SameSite=Strict`;
}

function clearDashboardCookie(): string {
  return `${DASHBOARD_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict`;
}

function sameOriginForm(request: Request, env: Env): boolean {
  const formOrigin = request.headers.get("origin");
  return !formOrigin || formOrigin === origin(env);
}

async function loginAttemptAllowed(request: Request, env: Env): Promise<boolean> {
  if (!dashboardSecretConfigured(env)) return false;
  const now = Date.now();
  const windowStart = new Date(Math.floor(now / DASHBOARD_LOGIN_WINDOW_MS) * DASHBOARD_LOGIN_WINDOW_MS).toISOString().replace(/\.\d{3}Z$/u, "Z");
  const rateKey = await hmac({ purpose: "publik_dashboard_login_rate", window_start: windowStart, edge_ip: request.headers.get("cf-connecting-ip") || "missing" }, env.DASHBOARD_ACCESS_SECRET as string);
  const row = await env.DB.prepare(
    "INSERT INTO dashboard_login_rate (rate_key, window_start, attempts, updated_at) VALUES (?, ?, 1, ?) ON CONFLICT(rate_key) DO UPDATE SET attempts = dashboard_login_rate.attempts + 1, updated_at = excluded.updated_at RETURNING attempts"
  ).bind(rateKey, windowStart, isoNow()).first<CountRow>();
  return Number(row?.count || row?.attempts || 0) <= DASHBOARD_LOGIN_ATTEMPTS;
}

const DASHBOARD_LOGIN_HTML = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Publik dashboard sign in</title></head><body><main><h1>Dashboard sign in</h1><form method="post" action="/dashboard/login"><label>Access secret <input name="secret" type="password" autocomplete="current-password" required></label><button type="submit">Sign in</button></form></main></body></html>`;

async function dashboardLogin(request: Request, env: Env): Promise<Response> {
  if (!dashboardSecretConfigured(env)) return new Response("Unauthorized", { status: 401, headers: NO_STORE });
  if (request.method === "GET") return new Response(DASHBOARD_LOGIN_HTML, { headers: { ...NO_STORE, "content-type": "text/html; charset=utf-8", "content-security-policy": "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'; style-src 'unsafe-inline'" } });
  if (request.method !== "POST") return methodNotAllowed();
  if (!sameOriginForm(request, env)) return denied();
  if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/x-www-form-urlencoded") return badRequest();
  const length = Number(request.headers.get("content-length") || "0");
  if (!Number.isFinite(length) || length > 1024) return badRequest();
  try {
    if (!await loginAttemptAllowed(request, env)) return new Response("Too Many Requests", { status: 429, headers: { ...NO_STORE, "retry-after": "300" } });
    const form = await request.formData();
    if ([...form.keys()].length !== 1 || typeof form.get("secret") !== "string") return badRequest();
    const presented = form.get("secret") as string;
    if (!await constantTimeEqual(presented, env.DASHBOARD_ACCESS_SECRET as string)) return new Response("Unauthorized", { status: 401, headers: NO_STORE });
    return new Response(null, { status: 303, headers: { ...NO_STORE, location: "/dashboard", "set-cookie": dashboardCookie(await mintDashboardSession(env)) } });
  } catch { return new Response("Dashboard unavailable", { status: 503, headers: NO_STORE }); }
}

function dashboardLogout(request: Request): Response {
  if (request.method !== "POST") return methodNotAllowed();
  return new Response(null, { status: 204, headers: { ...NO_STORE, "set-cookie": clearDashboardCookie() } });
}

async function dashboardAllowed(request: Request, env: Env): Promise<boolean> {
  return await accessAllowed(request, env) || await dashboardBearerAllowed(request, env) || await dashboardSessionAllowed(request, env);
}

async function dashboard(request: Request, env: Env, html: boolean): Promise<Response> {
  if (!await dashboardAllowed(request, env)) return new Response("Unauthorized", { status: 401, headers: NO_STORE });
  if (html) return new Response(DASHBOARD_HTML, { headers: { ...NO_STORE, "content-type": "text/html; charset=utf-8", "content-security-policy": "default-src 'self'; connect-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'" } });
  const windowStart = new Date(Date.now() - 30 * 86400000).toISOString().replace(/\.\d{3}Z$/u, "Z");
  const events = await env.DB.prepare("SELECT classification, event_type, action_id, occurred_at FROM events WHERE occurred_at >= ?").bind(windowStart).all<{ classification: string; event_type: string; action_id: string; occurred_at: string }>();
  const sent = await env.DB.prepare("SELECT COUNT(DISTINCT action_id) AS count FROM links WHERE activated_at IS NOT NULL AND (activated_at >= ? OR action_id IN (SELECT DISTINCT action_id FROM events WHERE occurred_at >= ?))").bind(windowStart, windowStart).first<CountRow>();
  const counts: Record<string, number> = { automation_likely: 0, unknown: 0, human_likely: 0 };
  const typeCounts: Record<string, Record<string, number>> = { open: { automation_likely: 0, unknown: 0, human_likely: 0 }, click: { automation_likely: 0, unknown: 0, human_likely: 0 }, deck_view: { automation_likely: 0, unknown: 0, human_likely: 0 } };
  const observed = new Set<string>();
  const humanClicks = new Set<string>();
  let latest: string | null = null;
  for (const event of events.results) {
    counts[event.classification] += 1;
    typeCounts[event.event_type][event.classification] += 1;
    observed.add(event.action_id);
    if (event.classification === "human_likely" && event.event_type === "click") humanClicks.add(event.action_id);
    latest = !latest || event.occurred_at > latest ? event.occurred_at : latest;
  }
  const asOf = isoNow();
  return json({ classification: "aggregate_only", window: { starts_at: windowStart, ends_at: asOf, retention_days: 30 }, sent_action_count: Number(sent?.count || 0), observed_unique_action_count: observed.size, human_likely_click_unique_action_count: humanClicks.size, classification_counts: counts, event_type_counts: typeCounts, freshness: { as_of: asOf, latest_event_at: latest, stale: !latest || Date.parse(asOf) - Date.parse(latest) > 86400000 }, interpretation: { opens_and_clicks_are_not_confirmed_people: true, scanner_events_remain_in_raw_total: true } });
}

async function actionSummary(request: Request, env: Env, actionId: string): Promise<Response> {
  if (!await dashboardAllowed(request, env)) return new Response("Unauthorized", { status: 401, headers: NO_STORE });
  try { requireOpaqueId(actionId, "action"); } catch { return denied(); }
  const activated = await env.DB.prepare("SELECT COUNT(*) AS count FROM links WHERE action_id = ? AND activated_at IS NOT NULL")
    .bind(actionId).first<CountRow>();
  if (!Number(activated?.count || 0)) return denied();
  const windowStart = new Date(Date.now() - 30 * 86400000).toISOString().replace(/\.\d{3}Z$/u, "Z");
  const events = await env.DB.prepare("SELECT classification, event_type, occurred_at FROM events WHERE action_id = ? AND occurred_at >= ?")
    .bind(actionId, windowStart).all<{ classification: string; event_type: string; occurred_at: string }>();
  const counts: Record<string, Record<string, number>> = {
    open: { automation_likely: 0, unknown: 0, human_likely: 0 },
    click: { automation_likely: 0, unknown: 0, human_likely: 0 },
    deck_view: { automation_likely: 0, unknown: 0, human_likely: 0 },
  };
  let latest: string | null = null;
  for (const event of events.results) {
    counts[event.event_type][event.classification] += 1;
    latest = !latest || event.occurred_at > latest ? event.occurred_at : latest;
  }
  return json({ classification: "action_only", action_id: actionId,
    window: { starts_at: windowStart, ends_at: isoNow(), retention_days: 30 },
    event_type_counts: counts, latest_event_at: latest,
    interpretation: { requests_are_not_confirmed_people: true, scanner_events_remain_separate: true } });
}

async function actionEvents(request: Request, env: Env, actionId: string): Promise<Response> {
  if (!await dashboardAllowed(request, env)) return new Response("Unauthorized", { status: 401, headers: NO_STORE });
  try { requireOpaqueId(actionId, "action"); } catch { return denied(); }
  const activatedRow = await env.DB.prepare("SELECT MIN(activated_at) AS earliest FROM links WHERE action_id = ? AND activated_at IS NOT NULL")
    .bind(actionId).first<{ earliest?: string | null }>();
  const activatedAt = activatedRow?.earliest || null;
  if (!activatedAt) return denied();
  const windowStart = new Date(Date.now() - 30 * 86400000).toISOString().replace(/\.\d{3}Z$/u, "Z");
  const rows = await env.DB.prepare(
    "SELECT occurred_at, event_type, source_kind, user_agent_family, network_class, classification, reason_codes_json FROM events WHERE action_id = ? AND occurred_at >= ? ORDER BY occurred_at DESC LIMIT 201"
  ).bind(actionId, windowStart).all<{ occurred_at: string; event_type: string; source_kind: string; user_agent_family: string; network_class: string; classification: string; reason_codes_json: string }>();
  const rowsResult = rows.results;
  const truncated = rowsResult.length > 200;
  const events = rowsResult.slice(0, 200).map((row) => {
    const diffMs = Date.parse(row.occurred_at) - Date.parse(activatedAt as string);
    return {
      occurred_at: row.occurred_at,
      event_type: row.event_type,
      source_kind: row.source_kind,
      user_agent_family: row.user_agent_family,
      network_class: row.network_class,
      classification: row.classification,
      reason_codes: JSON.parse(row.reason_codes_json),
      seconds_since_send: Number.isFinite(diffMs) ? Math.round(diffMs / 1000) : null,
    };
  });
  return json({
    classification: "action_events",
    action_id: actionId,
    activated_at: activatedAt,
    window: { starts_at: windowStart, ends_at: isoNow(), retention_days: 30 },
    events,
    truncated,
  });
}

async function purge(env: Env): Promise<void> {
  const cutoff = new Date(Date.now() - 30 * 86400000).toISOString().replace(/\.\d{3}Z$/u, "Z");
  const rateCutoff = new Date(Date.now() - 15 * 60 * 1000).toISOString().replace(/\.\d{3}Z$/u, "Z");
  try {
    const results = await env.DB.batch([
      env.DB.prepare("DELETE FROM events WHERE occurred_at < ?").bind(cutoff),
      env.DB.prepare("DELETE FROM dashboard_login_rate WHERE updated_at < ?").bind(rateCutoff),
    ]);
    if (results.length !== 2 || results.some((result) => !result.success)) throw new Error("retention cleanup failed");
  } catch { throw new Error("retention cleanup failed"); }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    try { if (url.origin !== origin(env)) return denied(); } catch { return denied(); }
    if (url.pathname === "/internal/links") return request.method === "POST" ? mintLink(request, env) : methodNotAllowed();
    if (url.pathname === "/internal/activate") return request.method === "POST" ? activateLink(request, env) : methodNotAllowed();
    if (url.pathname.startsWith("/r/")) return request.method === "GET" ? redirect(request, env, url, url.pathname.slice(3)) : methodNotAllowed();
    if (url.pathname.startsWith("/o/")) return openPixel(request, env, url, url.pathname.slice(3));
    if (url.pathname.startsWith("/d/")) return deckView(request, env, url, url.pathname.slice(3));
    if (url.pathname.startsWith("/u/")) return unsubscribe(request, env, url, url.pathname.slice(3));
    if (url.pathname === "/dashboard/login") return dashboardLogin(request, env);
    if (url.pathname === "/dashboard/logout") return dashboardLogout(request);
    if (url.pathname === "/dashboard") return request.method === "GET" ? dashboard(request, env, true) : methodNotAllowed();
    if (url.pathname === "/api/dashboard") return request.method === "GET" ? dashboard(request, env, false) : methodNotAllowed();
    if (url.pathname.startsWith("/api/actions/")) {
      const rest = url.pathname.slice("/api/actions/".length);
      if (rest.endsWith("/events")) {
        if (request.method !== "GET") return methodNotAllowed();
        const actionId = rest.slice(0, -"/events".length);
        return actionId.includes("/") ? denied() : actionEvents(request, env, actionId);
      }
      if (request.method !== "GET") return methodNotAllowed();
      return rest.includes("/") ? denied() : actionSummary(request, env, rest);
    }
    return denied();
  },
  async scheduled(_event: ScheduledEvent, env: Env): Promise<void> { await purge(env); },
};
