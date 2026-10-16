import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import worker from "../src/worker.ts";
import { canonicalJson, expectedP39LinkHash, hmac, sha256, signedDeckUrl, signedLinkUrl, signedOpenUrl, signedUnsubscribeUrl } from "../src/logic.mjs";

const PUBLIC = "https://track.example.test";
const INTERNAL = "internal-test-key-must-be-at-least-32-chars";
const TRACKING = "tracking-test-key-must-be-at-least-32-chars";
const ACTIVATION = "activation-test-key-must-be-at-least-32";

class FakeStatement {
  constructor(db, query, values = []) { this.db = db; this.query = query.toLowerCase().replace(/\s+/gu, " ").trim(); this.values = values; }
  bind(...values) { return new FakeStatement(this.db, this.query, values); }
  async first() {
    if (this.query.startsWith("select link_id")) return this.db.links.get(this.values[0]) || null;
    if (this.query.startsWith("select count(*) as count from events")) {
      const [action, since] = this.values;
      return { count: this.db.events.filter((event) => event.action_id === action && event.occurred_at >= since).length };
    }
    if (this.query.includes("count(distinct action_id)")) {
      const [windowStart] = this.values;
      const observed = new Set(this.db.events.filter((event) => event.occurred_at >= windowStart).map((event) => event.action_id));
      return { count: new Set([...this.db.links.values()].filter((link) => link.activated_at && (link.activated_at >= windowStart || observed.has(link.action_id))).map((link) => link.action_id)).size };
    }
    if (this.query.startsWith("select count(*) as count from links where action_id")) {
      return { count: [...this.db.links.values()].filter((link) => link.action_id === this.values[0] && link.activated_at).length };
    }
    if (this.query.startsWith("select min(activated_at) as earliest from links")) {
      const [action] = this.values;
      const activatedDates = [...this.db.links.values()].filter((link) => link.action_id === action && link.activated_at).map((link) => link.activated_at).sort();
      return { earliest: activatedDates[0] || null };
    }
    if (this.query.startsWith("insert into dashboard_login_rate")) {
      const [rate_key, window_start, updated_at] = this.values;
      const current = this.db.loginRates.get(rate_key) || { rate_key, window_start, attempts: 0, updated_at };
      current.attempts += 1;
      current.updated_at = updated_at;
      this.db.loginRates.set(rate_key, current);
      return { attempts: current.attempts };
    }
    throw new Error(`unexpected first query: ${this.query}`);
  }
  async all() {
    if (this.query.startsWith("select classification, event_type, action_id, occurred_at from events")) return { results: this.db.events.filter((event) => event.occurred_at >= this.values[0]).map(({ classification, event_type, action_id, occurred_at }) => ({ classification, event_type, action_id, occurred_at })) };
    if (this.query.startsWith("select classification, event_type, occurred_at from events where action_id")) return { results: this.db.events.filter((event) => event.action_id === this.values[0] && event.occurred_at >= this.values[1]).map(({ classification, event_type, occurred_at }) => ({ classification, event_type, occurred_at })) };
    if (this.query.startsWith("select occurred_at, event_type, source_kind, user_agent_family, network_class, classification, reason_codes_json from events")) {
      const [action, since] = this.values;
      const rows = this.db.events
        .filter((event) => event.action_id === action && event.occurred_at >= since)
        .map(({ occurred_at, event_type, source_kind, user_agent_family, network_class, classification, reason_codes_json }) => ({ occurred_at, event_type, source_kind, user_agent_family, network_class, classification, reason_codes_json }))
        .sort((a, b) => (a.occurred_at < b.occurred_at ? 1 : a.occurred_at > b.occurred_at ? -1 : 0))
        .slice(0, 201);
      return { results: rows };
    }
    throw new Error(`unexpected all query: ${this.query}`);
  }
  async run() {
    if (this.query.startsWith("insert into links")) {
      const [link_id, campaign_id, action_id, destination_key, link_hash, expires_at, created_at] = this.values;
      if (this.db.links.has(link_id)) return { success: false, meta: { changes: 0 } };
      this.db.links.set(link_id, { link_id, campaign_id, action_id, destination_key, link_hash, expires_at, activated_at: null, receipt_hash: null, created_at });
      return { success: true, meta: { changes: 1 } };
    }
    if (this.query.startsWith("update links set activated_at")) {
      const [activated_at, receipt_hash, link_id] = this.values;
      const link = this.db.links.get(link_id);
      if (!link || link.activated_at || link.receipt_hash) return { success: true, meta: { changes: 0 } };
      link.activated_at = activated_at;
      link.receipt_hash = receipt_hash;
      return { success: true, meta: { changes: 1 } };
    }
    if (this.query.startsWith("insert or ignore into events")) {
      const [event_id, link_id, action_id, occurred_at, hour_bucket, event_type, source_kind, user_agent_family, network_class, machine_cadence, fanout_count, event_fingerprint_hash, classification, reason_codes_json, created_at] = this.values;
      const capped = this.db.events.filter((event) => event.action_id === action_id && event.hour_bucket === hour_bucket).length >= 20;
      const duplicate = this.db.events.some((event) => event.event_id === event_id || event.event_fingerprint_hash === event_fingerprint_hash);
      if (capped || duplicate) return { success: true, meta: { changes: 0 } };
      this.db.events.push({ event_id, link_id, action_id, occurred_at, hour_bucket, event_type, source_kind, user_agent_family, network_class, machine_cadence, fanout_count, event_fingerprint_hash, classification, reason_codes_json, created_at });
      return { success: true, meta: { changes: 1 } };
    }
    if (this.query.startsWith("insert or ignore into unsubscribe_denials")) {
      const [action_id, link_id, received_at, idempotency_fingerprint_hash, deny_mac, created_at] = this.values;
      if (!this.db.denials.has(action_id)) this.db.denials.set(action_id, { action_id, link_id, received_at, idempotency_fingerprint_hash, deny_mac, created_at });
      return { success: true, meta: { changes: this.db.denials.get(action_id).idempotency_fingerprint_hash === idempotency_fingerprint_hash ? 1 : 0 } };
    }
    if (this.query.startsWith("insert or ignore into suppression_outbox")) {
      const [action_id, denial_fingerprint_hash, deny_mac, created_at] = this.values;
      if (!this.db.outbox.has(action_id)) this.db.outbox.set(action_id, { action_id, denial_fingerprint_hash, deny_mac, created_at });
      return { success: true, meta: { changes: 1 } };
    }
    if (this.query.startsWith("delete from events")) {
      if (this.db.failDelete) return { success: false, meta: { changes: 0 } };
      this.db.events = this.db.events.filter((event) => event.occurred_at >= this.values[0]);
      return { success: true, meta: { changes: 1 } };
    }
    if (this.query.startsWith("delete from dashboard_login_rate")) {
      if (this.db.failDelete) return { success: false, meta: { changes: 0 } };
      this.db.loginRates = new Map([...this.db.loginRates].filter(([, value]) => value.updated_at >= this.values[0]));
      return { success: true, meta: { changes: 1 } };
    }
    throw new Error(`unexpected run query: ${this.query}`);
  }
}

class FakeD1 {
  constructor() { this.links = new Map(); this.events = []; this.denials = new Map(); this.outbox = new Map(); this.loginRates = new Map(); this.batchCalls = 0; this.failBatch = false; this.failDelete = false; }
  prepare(query) { return new FakeStatement(this, query); }
  async batch(statements) { this.batchCalls += 1; if (this.failBatch) throw new Error("D1 unavailable"); return Promise.all(statements.map((statement) => statement.run())); }
}

class FakeAssets {
  constructor(bytes = new TextEncoder().encode("%PDF-1.4 fake deck bytes for tests"), status = 200) {
    this.bytes = bytes;
    this.status = status;
    this.requests = [];
  }
  async fetch(request) {
    this.requests.push(request);
    if (this.status !== 200) return new Response(null, { status: this.status });
    return new Response(this.bytes, { status: 200, headers: { "content-type": "application/octet-stream" } });
  }
}

async function accessEnv() {
  const pair = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
  const jwk = await crypto.subtle.exportKey("jwk", pair.publicKey);
  jwk.kid = "test-key";
  return { pair, jwks: JSON.stringify({ keys: [jwk] }) };
}

function environment(db, jwks = "{\"keys\":[]}", dashboardSecret = undefined, assets = undefined) {
  return { DB: db, PUBLIC_ORIGIN: PUBLIC, DESTINATION_ORIGINS: "https://publik.example.test", DESTINATION_MAP_JSON: JSON.stringify({ publik_home: "https://publik.example.test/demo" }), TRACKING_SIGNING_KEY: TRACKING, INTERNAL_API_KEY: INTERNAL, ACTIVATION_SIGNING_KEY: ACTIVATION, ACCESS_TEAM_DOMAIN: "access.example.test", ACCESS_AUD: "access-audience", ACCESS_JWKS_JSON: jwks, DASHBOARD_ACCESS_SECRET: dashboardSecret, ASSETS: assets };
}

const DASHBOARD_SECRET = "dashboard-access-test-secret-must-be-at-least-32-chars";

function ids(number = "01") {
  return { campaign_id: `campaign_${"0".repeat(30)}${number}`, action_id: `action_${"0".repeat(30)}${number}`, link_id: `link_${"0".repeat(30)}${number}`, destination_key: "publik_home", expires_at: "2099-01-01T00:00:00Z" };
}

async function p39(values) { return { ...values, link_hash: await expectedP39LinkHash(values, PUBLIC, TRACKING) }; }

async function receipt(action_id) {
  const value = { schema_version: "1", receipt_kind: "publik_gmail_test_provider_result", action_id, manifest_hash: "sha256:" + "a".repeat(64), local_execution_receipt_hash: "sha256:" + "b".repeat(64), request_hash: "sha256:" + "c".repeat(64), message_sha256: "sha256:" + "d".repeat(64), message_id_sha256: "sha256:" + "e".repeat(64), outcome: "success", executed_at: "2026-07-30T23:00:00Z", provider_message_id_hash: "sha256:" + "f".repeat(64), retry_disposition: "no_retry_needed", automatic_retry_authorized: false, network_accessed: true, live_recipient_used: false, authority: { automation_promotion_authorized: false, arbitrary_recipient_authorized: false, delivery_authorized: true, external_actions_authorized: true, external_use_authorized: false, live_delivery_authorized: false, network_access_authorized: true, provider_send_authorized: true, submission_authorized: false, test_delivery_authorized: true, tracking_authorized: false } };
  value.receipt_hash = await sha256(canonicalJson(value));
  return value;
}

async function manualLiveReceipt(action_id) {
  const value = { schema_version: "1", receipt_kind: "publik_gmail_manual_live_provider_result", action_id, manifest_hash: "sha256:" + "a".repeat(64), local_execution_receipt_hash: "sha256:" + "b".repeat(64), request_hash: "sha256:" + "c".repeat(64), message_sha256: "sha256:" + "d".repeat(64), message_id_sha256: "sha256:" + "e".repeat(64), outcome: "success", executed_at: "2026-08-03T20:00:00Z", provider_message_id_hash: "sha256:" + "f".repeat(64), retry_disposition: "no_retry_needed", automatic_retry_authorized: false, network_accessed: true, live_recipient_used: true, authority: { automation_promotion_authorized: false, arbitrary_recipient_authorized: false, delivery_authorized: true, external_actions_authorized: true, external_use_authorized: true, live_delivery_authorized: true, network_access_authorized: true, provider_send_authorized: true, submission_authorized: false, test_delivery_authorized: false, tracking_authorized: true } };
  value.receipt_hash = await sha256(canonicalJson(value));
  return value;
}

async function mint(env, values) {
  const response = await worker.fetch(new Request(`${PUBLIC}/internal/links`, { method: "POST", headers: { "content-type": "application/json", "x-publik-internal-key": INTERNAL }, body: JSON.stringify(values) }), env);
  assert.equal(response.status, 201);
  return response.json();
}

async function activationMac(values, p18) {
  const claims = { accepted_at: p18.executed_at, action_id: values.action_id, link_hash: values.link_hash, link_id: values.link_id, receipt_hash: p18.receipt_hash, receipt_id: `receipt_${p18.receipt_hash.slice("sha256:".length, "sha256:".length + 32)}` };
  return hmac(claims, ACTIVATION);
}

async function activationResponse(env, values, providerReceipt) {
  return worker.fetch(new Request(`${PUBLIC}/internal/activate`, { method: "POST", headers: { "content-type": "application/json", "x-publik-internal-key": INTERNAL, "x-publik-activation-mac": await activationMac(values, providerReceipt) }, body: JSON.stringify({ link_id: values.link_id, receipt: providerReceipt }) }), env);
}

async function activate(env, values) {
  const p18 = await receipt(values.action_id);
  const response = await activationResponse(env, values, p18);
  assert.equal(response.status, 201);
}

async function accessToken(pair, overrides = {}, kid = "test-key") {
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const header = encode({ alg: "RS256", kid });
  const payload = encode({ iss: "https://access.example.test", aud: ["access-audience"], exp: Math.floor(Date.now() / 1000) + 60, ...overrides });
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", pair.privateKey, new TextEncoder().encode(`${header}.${payload}`));
  return `${header}.${payload}.${Buffer.from(signature).toString("base64url")}`;
}

test("worker entrypoint binds the exact origin and fail-closed internal authorization", async () => {
  const env = environment(new FakeD1());
  const values = await p39(ids());
  const shortKey = await worker.fetch(new Request(`${PUBLIC}/internal/links`, { method: "POST", headers: { "content-type": "application/json", "x-publik-internal-key": "short" }, body: JSON.stringify(values) }), env);
  assert.equal(shortKey.status, 404);
  const wrongOrigin = await worker.fetch(new Request(`https://wrong.example.test/internal/links`, { method: "POST", headers: { "content-type": "application/json", "x-publik-internal-key": INTERNAL }, body: JSON.stringify(values) }), env);
  assert.equal(wrongOrigin.status, 404);
  const minted = await mint(env, values);
  assert.match(minted.redirect_url, /\/r\/link_/u);
  assert.match(minted.open_url, /\/o\/link_/u);
  assert.match(minted.unsubscribe_url, /\/u\/link_/u);
  const p39Mismatch = await worker.fetch(new Request(`${PUBLIC}/internal/links`, { method: "POST", headers: { "content-type": "application/json", "x-publik-internal-key": INTERNAL }, body: JSON.stringify({ ...await p39(ids("09")), link_hash: "sha256:" + "0".repeat(64) }) }), env);
  assert.equal(p39Mismatch.status, 400);
});

test("P18 activation gates signed open and click ingestion, with no HEAD persistence", async () => {
  const db = new FakeD1();
  const env = environment(db);
  const values = await p39(ids());
  const minted = await mint(env, values);
  assert.equal((await worker.fetch(new Request(minted.redirect_url), env)).status, 404);
  await activate(env, values);
  const click = await worker.fetch(new Request(minted.redirect_url, { headers: { "user-agent": "Mozilla/5.0 Chrome/120.0", "cf-connecting-ip": "198.51.100.1" } }), env);
  assert.equal(click.status, 302);
  assert.equal(click.headers.get("location"), "https://publik.example.test/demo");
  assert.equal(click.headers.get("cache-control"), "no-store");
  assert.equal(click.headers.get("referrer-policy"), "no-referrer");
  assert.equal((await worker.fetch(new Request(minted.open_url, { headers: { "user-agent": "Mozilla/5.0 Chrome/120.0", "cf-connecting-ip": "198.51.100.1" } }), env)).status, 200);
  assert.equal((await worker.fetch(new Request(minted.open_url, { method: "HEAD" }), env)).status, 204);
  assert.equal((await worker.fetch(new Request(`${minted.open_url}&x=1`), env)).status, 404);
  assert.equal((await worker.fetch(new Request(minted.redirect_url, { method: "POST" }), env)).status, 405);
  assert.deepEqual(db.events.map((event) => event.event_type).sort(), ["click", "open"]);
  assert.ok(db.events.every((event) => !Object.hasOwn(event, "ip") && !Object.hasOwn(event, "user_agent")));
});

test("expired telemetry does not break hosted destinations or one-click unsubscribe", async () => {
  const db = new FakeD1();
  const env = environment(db);
  const values = await p39(ids("06"));
  await mint(env, values);
  await activate(env, values);
  const link = db.links.get(values.link_id);
  link.expires_at = "2020-01-01T00:00:00Z";
  const redirectUrl = await signedLinkUrl(link, PUBLIC, TRACKING);
  const openUrl = await signedOpenUrl(link, PUBLIC, TRACKING);
  const unsubscribeUrl = await signedUnsubscribeUrl(link, PUBLIC, TRACKING);
  const redirect = await worker.fetch(new Request(redirectUrl), env);
  assert.equal(redirect.status, 302);
  assert.equal(redirect.headers.get("location"), "https://publik.example.test/demo");
  assert.equal((await worker.fetch(new Request(openUrl), env)).status, 200);
  assert.equal(db.events.length, 0);
  const unsubscribe = await worker.fetch(new Request(unsubscribeUrl, {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ "List-Unsubscribe": "One-Click" }),
  }), env);
  assert.equal(unsubscribe.status, 204);
  assert.equal(db.denials.size, 1);
});

test("D1 unique fingerprints and the 20-event action-hour cap survive cross-request attempts", async () => {
  const db = new FakeD1();
  const env = environment(db);
  const values = await p39(ids());
  const minted = await mint(env, values);
  await activate(env, values);
  for (let index = 0; index < 25; index += 1) await worker.fetch(new Request(minted.redirect_url, { headers: { "user-agent": `Mozilla/5.0 Chrome/120.${index}`, "cf-connecting-ip": `198.51.100.${index}` } }), env);
  assert.equal(db.events.length, 20);
  assert.equal(new Set(db.events.map((event) => event.event_fingerprint_hash)).size, 20);
  assert.equal((await worker.fetch(new Request(minted.redirect_url, { headers: { "user-agent": "Mozilla/5.0 Chrome/120.0", "cf-connecting-ip": "198.51.100.0" } }), env)).status, 302);
  assert.equal(db.events.length, 20);
});

test("protected action summary returns only classified counts for one activated action", async () => {
  const db = new FakeD1();
  const env = environment(db, "{\"keys\":[]}", DASHBOARD_SECRET);
  const first = await p39(ids("01"));
  const second = await p39(ids("02"));
  const firstUrls = await mint(env, first);
  const secondUrls = await mint(env, second);
  await activate(env, first);
  await activate(env, second);
  await worker.fetch(new Request(firstUrls.open_url, { headers: { "user-agent": "GoogleImageProxy", "cf-connecting-ip": "198.51.100.1" } }), env);
  await worker.fetch(new Request(secondUrls.redirect_url, { headers: { "user-agent": "Mozilla/5.0 Chrome/120.0", "cf-connecting-ip": "198.51.100.2" } }), env);
  const url = `${PUBLIC}/api/actions/${first.action_id}`;
  assert.equal((await worker.fetch(new Request(url), env)).status, 401);
  assert.equal((await worker.fetch(new Request(`${PUBLIC}/api/actions/action_bad`, { headers: { authorization: `Bearer ${DASHBOARD_SECRET}` } }), env)).status, 404);
  const response = await worker.fetch(new Request(url, { headers: { authorization: `Bearer ${DASHBOARD_SECRET}` } }), env);
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.classification, "action_only");
  assert.equal(payload.action_id, first.action_id);
  assert.equal(payload.event_type_counts.open.automation_likely, 1);
  assert.equal(payload.event_type_counts.click.automation_likely, 0);
  assert.equal(payload.event_type_counts.click.unknown, 0);
  assert.equal(payload.interpretation.requests_are_not_confirmed_people, true);
  assert.equal(JSON.stringify(payload).includes("198.51.100"), false);
  assert.equal((await worker.fetch(new Request(url, { method: "POST", headers: { authorization: `Bearer ${DASHBOARD_SECRET}` } }), env)).status, 405);
});

test("RFC 8058 POST creates an atomic MAC-attested opaque denial and GET cannot suppress", async () => {
  const db = new FakeD1();
  const env = environment(db);
  const values = await p39(ids());
  const minted = await mint(env, values);
  assert.equal((await worker.fetch(new Request(minted.unsubscribe_url), env)).status, 405);
  const body = new URLSearchParams({ "List-Unsubscribe": "One-Click" });
  assert.equal((await worker.fetch(new Request(minted.unsubscribe_url, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body }), env)).status, 204);
  assert.equal(db.batchCalls, 1);
  assert.equal(db.denials.size, 1);
  assert.equal(db.outbox.size, 1);
  assert.equal(db.denials.get(values.action_id).deny_mac, db.outbox.get(values.action_id).deny_mac);
});

test("unsubscribe storage outages are retryable and retention failures surface to the scheduler", async () => {
  const db = new FakeD1();
  const env = environment(db);
  const values = await p39(ids());
  const minted = await mint(env, values);
  db.failBatch = true;
  const response = await worker.fetch(new Request(minted.unsubscribe_url, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ "List-Unsubscribe": "One-Click" }) }), env);
  assert.equal(response.status, 503);
  assert.equal(db.denials.size, 0);
  assert.equal(db.outbox.size, 0);
  db.failDelete = true;
  await assert.rejects(worker.scheduled({}, env), /retention cleanup failed/u);
});

test("Access-protected aggregate dashboard has no CORS, distinct sent-action denominator, and scheduled retention", async () => {
  const db = new FakeD1();
  const access = await accessEnv();
  const env = environment(db, access.jwks);
  const first = await p39(ids("01"));
  const second = await p39({ ...ids("02"), action_id: first.action_id });
  const firstMinted = await mint(env, first);
  const secondMinted = await mint(env, second);
  await activate(env, first);
  await activate(env, second);
  await worker.fetch(new Request(firstMinted.open_url, { headers: { "user-agent": "GoogleImageProxy", "cf-connecting-ip": "198.51.100.1" } }), env);
  await worker.fetch(new Request(secondMinted.redirect_url, { headers: { "user-agent": "Mozilla/5.0 Chrome/120.0", "cf-connecting-ip": "198.51.100.2" } }), env);
  db.links.set("link_00000000000000000000000000000008", { ...await p39(ids("08")), activated_at: "2020-01-01T00:00:00Z", receipt_hash: "sha256:" + "a".repeat(64) });
  db.events.push({ action_id: "action_00000000000000000000000000000008", occurred_at: "2020-01-01T00:00:00Z", event_type: "open", classification: "unknown" });
  const unauthenticated = await worker.fetch(new Request(`${PUBLIC}/api/dashboard`), env);
  assert.equal(unauthenticated.status, 401);
  assert.equal(unauthenticated.headers.get("access-control-allow-origin"), null);
  const dashboard = await worker.fetch(new Request(`${PUBLIC}/api/dashboard`, { headers: { "cf-access-jwt-assertion": await accessToken(access.pair) } }), env);
  assert.equal(dashboard.status, 200);
  assert.equal(dashboard.headers.get("access-control-allow-origin"), null);
  const aggregate = await dashboard.json();
  assert.equal(aggregate.sent_action_count, 1);
  assert.equal(aggregate.window.retention_days, 30);
  assert.ok(aggregate.window.starts_at < aggregate.window.ends_at);
  assert.equal(aggregate.event_type_counts.open.automation_likely, 1);
  db.events.push({ action_id: first.action_id, occurred_at: "2020-01-01T00:00:00Z" });
  await worker.scheduled({}, env);
  assert.ok(db.events.every((event) => event.occurred_at >= "2026-06-30T00:00:00Z"));
});

test("dashboard denominator includes every observed action inside its window", async () => {
  const db = new FakeD1();
  const access = await accessEnv();
  const env = environment(db, access.jwks);
  const oldLink = await p39(ids("07"));
  db.links.set(oldLink.link_id, { ...oldLink, activated_at: "2020-01-01T00:00:00Z", receipt_hash: "sha256:" + "b".repeat(64) });
  db.events.push({ action_id: oldLink.action_id, occurred_at: new Date().toISOString().replace(/\.\d{3}Z$/u, "Z"), event_type: "click", classification: "unknown" });
  const response = await worker.fetch(new Request(`${PUBLIC}/api/dashboard`, { headers: { "cf-access-jwt-assertion": await accessToken(access.pair) } }), env);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).sent_action_count, 1);
});

test("dashboard Access JWT rejects wrong issuer, audience, expiry, signature, and key ID", async () => {
  const access = await accessEnv();
  const env = environment(new FakeD1(), access.jwks);
  const invalid = [
    await accessToken(access.pair, { aud: ["wrong-audience"] }),
    await accessToken(access.pair, { iss: "https://wrong.example.test" }),
    await accessToken(access.pair, { exp: Math.floor(Date.now() / 1000) - 1 }),
    await accessToken(access.pair, {}, "unknown-key"),
  ];
  const valid = await accessToken(access.pair);
  invalid.push(`${valid.slice(0, -1)}${valid.endsWith("a") ? "b" : "a"}`);
  for (const token of invalid) assert.equal((await worker.fetch(new Request(`${PUBLIC}/api/dashboard`, { headers: { "cf-access-jwt-assertion": token } }), env)).status, 401);
});

test("staging dashboard fallback uses only a constant-time bearer or short-lived HttpOnly session, never a URL credential", async () => {
  const db = new FakeD1();
  const env = environment(db, "{\"keys\":[]}", DASHBOARD_SECRET);
  const denied = await worker.fetch(new Request(`${PUBLIC}/api/dashboard?secret=${DASHBOARD_SECRET}`), env);
  assert.equal(denied.status, 401);
  assert.equal(await denied.text(), "Unauthorized");
  assert.equal(denied.headers.get("access-control-allow-origin"), null);
  const wrongBearer = await worker.fetch(new Request(`${PUBLIC}/api/dashboard`, { headers: { authorization: "Bearer incorrect" } }), env);
  assert.equal(wrongBearer.status, 401);
  const bearer = await worker.fetch(new Request(`${PUBLIC}/api/dashboard`, { headers: { authorization: `Bearer ${DASHBOARD_SECRET}` } }), env);
  assert.equal(bearer.status, 200);
  const loginPage = await worker.fetch(new Request(`${PUBLIC}/dashboard/login`), env);
  assert.equal(loginPage.status, 200);
  assert.equal(loginPage.headers.get("cache-control"), "no-store");
  assert.doesNotMatch(await loginPage.text(), /dashboard-access-test-secret/u);
  const login = await worker.fetch(new Request(`${PUBLIC}/dashboard/login`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", origin: PUBLIC, "cf-connecting-ip": "198.51.100.10" }, body: new URLSearchParams({ secret: DASHBOARD_SECRET }) }), env);
  assert.equal(login.status, 303);
  const cookie = login.headers.get("set-cookie");
  assert.match(cookie, /^__Host-publik_dashboard=/u);
  assert.match(cookie, /Max-Age=900; HttpOnly; Secure; SameSite=Strict$/u);
  assert.doesNotMatch(cookie, /dashboard-access-test-secret/u);
  const session = await worker.fetch(new Request(`${PUBLIC}/dashboard`, { headers: { cookie: cookie.split(";", 1)[0] } }), env);
  assert.equal(session.status, 200);
  const logout = await worker.fetch(new Request(`${PUBLIC}/dashboard/logout`, { method: "POST", headers: { cookie: cookie.split(";", 1)[0] } }), env);
  assert.equal(logout.status, 204);
  assert.match(logout.headers.get("set-cookie"), /Max-Age=0; HttpOnly; Secure; SameSite=Strict$/u);
  const crossOrigin = await worker.fetch(new Request(`${PUBLIC}/dashboard/login`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", origin: "https://attacker.example.test", "cf-connecting-ip": "198.51.100.11" }, body: new URLSearchParams({ secret: DASHBOARD_SECRET }) }), env);
  assert.equal(crossOrigin.status, 404);
});

test("staging dashboard login throttles HMAC-pseudonymized attempts and keeps Access-only mode fail closed when unset", async () => {
  const db = new FakeD1();
  const env = environment(db, "{\"keys\":[]}", DASHBOARD_SECRET);
  for (let index = 0; index < 5; index += 1) {
    const response = await worker.fetch(new Request(`${PUBLIC}/dashboard/login`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", origin: PUBLIC, "cf-connecting-ip": "198.51.100.12" }, body: new URLSearchParams({ secret: "wrong" }) }), env);
    assert.equal(response.status, 401);
  }
  const throttled = await worker.fetch(new Request(`${PUBLIC}/dashboard/login`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", origin: PUBLIC, "cf-connecting-ip": "198.51.100.12" }, body: new URLSearchParams({ secret: DASHBOARD_SECRET }) }), env);
  assert.equal(throttled.status, 429);
  assert.equal(throttled.headers.get("retry-after"), "300");
  assert.equal(db.loginRates.size, 1);
  const access = await accessEnv();
  const accessOnly = environment(new FakeD1(), access.jwks);
  assert.equal((await worker.fetch(new Request(`${PUBLIC}/dashboard/login`), accessOnly)).status, 401);
  assert.equal((await worker.fetch(new Request(`${PUBLIC}/api/dashboard`, { headers: { "cf-access-jwt-assertion": await accessToken(access.pair) } }), accessOnly)).status, 200);
});

test("activation MAC binds a P18 receipt to exactly one P39 link", async () => {
  const db = new FakeD1();
  const env = environment(db);
  const first = await p39(ids("03"));
  const second = await p39({ ...ids("04"), action_id: first.action_id });
  await mint(env, first);
  await mint(env, second);
  const p18 = await receipt(first.action_id);
  const firstMac = await activationMac(first, p18);
  const replay = await worker.fetch(new Request(`${PUBLIC}/internal/activate`, { method: "POST", headers: { "content-type": "application/json", "x-publik-internal-key": INTERNAL, "x-publik-activation-mac": firstMac }, body: JSON.stringify({ link_id: second.link_id, receipt: p18 }) }), env);
  assert.equal(replay.status, 400);
  assert.equal(db.links.get(second.link_id).activated_at, null);
});

test("internal activation accepts only the exact manual-live receipt and preserves the P18 test path", async () => {
  const db = new FakeD1();
  const env = environment(db);
  const testValues = await p39(ids("10"));
  const liveValues = await p39(ids("11"));
  const testRoutedValues = await p39(ids("12"));
  const widenedValues = await p39(ids("13"));
  for (const values of [testValues, liveValues, testRoutedValues, widenedValues]) await mint(env, values);

  const testReceipt = await receipt(testValues.action_id);
  assert.equal((await activationResponse(env, testValues, testReceipt)).status, 201);
  assert.equal(db.links.get(testValues.link_id).receipt_hash, testReceipt.receipt_hash);

  const liveReceipt = await manualLiveReceipt(liveValues.action_id);
  assert.equal((await activationResponse(env, liveValues, liveReceipt)).status, 201);
  assert.equal(db.links.get(liveValues.link_id).receipt_hash, liveReceipt.receipt_hash);

  const testRouted = await manualLiveReceipt(testRoutedValues.action_id);
  testRouted.live_recipient_used = false;
  delete testRouted.receipt_hash;
  testRouted.receipt_hash = await sha256(canonicalJson(testRouted));
  assert.equal((await activationResponse(env, testRoutedValues, testRouted)).status, 400);
  assert.equal(db.links.get(testRoutedValues.link_id).activated_at, null);

  const widened = await manualLiveReceipt(widenedValues.action_id);
  widened.authority.arbitrary_recipient_authorized = true;
  delete widened.receipt_hash;
  widened.receipt_hash = await sha256(canonicalJson(widened));
  assert.equal((await activationResponse(env, widenedValues, widened)).status, 400);
  assert.equal(db.links.get(widenedValues.link_id).activated_at, null);
});

test("deck route serves the staged PDF for an activated link, records exactly one deck_view, and fails closed on bad or unactivated links", async () => {
  const db = new FakeD1();
  const assets = new FakeAssets();
  const env = environment(db, "{\"keys\":[]}", undefined, assets);
  const values = await p39(ids("20"));
  const minted = await mint(env, values);
  assert.match(minted.deck_url, /\/d\/link_/u);

  const beforeActivation = await worker.fetch(new Request(minted.deck_url), env);
  assert.equal(beforeActivation.status, 404);
  assert.equal(assets.requests.length, 0);

  await activate(env, values);

  const tail = minted.deck_url.at(-1);
  const badSignature = `${minted.deck_url.slice(0, -1)}${tail === "A" ? "B" : "A"}`;
  assert.equal((await worker.fetch(new Request(badSignature), env)).status, 404);
  assert.equal(db.events.length, 0);

  const response = await worker.fetch(new Request(minted.deck_url, { headers: { "user-agent": "Mozilla/5.0 Chrome/120.0", "cf-connecting-ip": "198.51.100.5" } }), env);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "application/pdf");
  assert.equal(response.headers.get("content-disposition"), "inline; filename=\"Publik_Pitch_Deck.pdf\"");
  assert.equal(response.headers.get("cache-control"), "private, no-store");
  assert.equal(response.headers.get("x-robots-tag"), "noindex");
  assert.equal(response.headers.get("referrer-policy"), "no-referrer");
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), assets.bytes);
  assert.equal(db.events.length, 1);
  assert.equal(db.events[0].event_type, "deck_view");

  const head = await worker.fetch(new Request(minted.deck_url, { method: "HEAD" }), env);
  assert.equal(head.status, 200);
  assert.equal(head.headers.get("content-type"), "application/pdf");
  assert.equal(await head.text(), "");
  assert.equal(db.events.length, 1);

  assert.equal((await worker.fetch(new Request(minted.deck_url, { method: "POST" }), env)).status, 405);
});

test("deck route stays servable after the telemetry window closes but stops recording", async () => {
  const db = new FakeD1();
  const assets = new FakeAssets();
  const env = environment(db, "{\"keys\":[]}", undefined, assets);
  const values = await p39(ids("21"));
  await mint(env, values);
  await activate(env, values);
  const link = db.links.get(values.link_id);
  link.expires_at = "2020-01-01T00:00:00Z";
  const deckUrl = await signedDeckUrl(link, PUBLIC, TRACKING);
  const response = await worker.fetch(new Request(deckUrl, { headers: { "user-agent": "Mozilla/5.0 Chrome/120.0" } }), env);
  assert.equal(response.status, 200);
  assert.equal(db.events.length, 0);
});

test("deck route falls back to the destination map when ASSETS is not bound, else denies", async () => {
  const db = new FakeD1();
  const values = await p39(ids("22"));
  const withFallback = { ...environment(db), DESTINATION_MAP_JSON: JSON.stringify({ publik_home: "https://publik.example.test/demo", deck: "https://publik.example.test/deck-fallback" }) };
  const minted = await mint(withFallback, values);
  await activate(withFallback, values);
  const fallback = await worker.fetch(new Request(minted.deck_url), withFallback);
  assert.equal(fallback.status, 302);
  assert.equal(fallback.headers.get("location"), "https://publik.example.test/deck-fallback");

  const noFallback = environment(new FakeD1());
  const values2 = await p39(ids("23"));
  const minted2 = await mint(noFallback, values2);
  await activate(noFallback, values2);
  assert.equal((await worker.fetch(new Request(minted2.deck_url), noFallback)).status, 404);
});

test("action events feed requires auth, hides PII, orders newest first, and caps at 200", async () => {
  const db = new FakeD1();
  const assets = new FakeAssets();
  const env = environment(db, "{\"keys\":[]}", DASHBOARD_SECRET, assets);
  const values = await p39(ids("24"));
  const minted = await mint(env, values);
  await activate(env, values);

  const url = `${PUBLIC}/api/actions/${values.action_id}/events`;
  assert.equal((await worker.fetch(new Request(url), env)).status, 401);

  const empty = await worker.fetch(new Request(url, { headers: { authorization: `Bearer ${DASHBOARD_SECRET}` } }), env);
  assert.equal(empty.status, 200);
  const emptyPayload = await empty.json();
  assert.equal(emptyPayload.classification, "action_events");
  assert.equal(emptyPayload.action_id, values.action_id);
  assert.equal(emptyPayload.activated_at, db.links.get(values.link_id).activated_at);
  assert.equal(emptyPayload.window.retention_days, 30);
  assert.equal(emptyPayload.events.length, 0);
  assert.equal(emptyPayload.truncated, false);

  await worker.fetch(new Request(minted.open_url, { headers: { "user-agent": "GoogleImageProxy", "cf-connecting-ip": "198.51.100.1" } }), env);
  await worker.fetch(new Request(minted.redirect_url, { headers: { "user-agent": "Mozilla/5.0 Chrome/120.0", "cf-connecting-ip": "198.51.100.2" } }), env);
  await worker.fetch(new Request(minted.deck_url, { headers: { "user-agent": "Mozilla/5.0 Chrome/120.0", "cf-connecting-ip": "198.51.100.3" } }), env);

  const response = await worker.fetch(new Request(url, { headers: { authorization: `Bearer ${DASHBOARD_SECRET}` } }), env);
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.events.length, 3);
  assert.deepEqual(payload.events.map((event) => event.event_type).sort(), ["click", "deck_view", "open"]);
  for (const event of payload.events) {
    assert.ok(Number.isInteger(event.seconds_since_send) || event.seconds_since_send === null);
    assert.ok(Array.isArray(event.reason_codes));
    assert.ok(["automation_likely", "unknown", "human_likely"].includes(event.classification));
    assert.ok(!Object.hasOwn(event, "link_id"));
    assert.ok(!Object.hasOwn(event, "user_agent"));
    assert.ok(!Object.hasOwn(event, "ip"));
  }
  const occurredTimes = payload.events.map((event) => event.occurred_at);
  assert.deepEqual(occurredTimes, [...occurredTimes].sort().reverse());
  const serialized = JSON.stringify(payload);
  assert.equal(serialized.includes("198.51.100"), false);
  assert.equal(serialized.includes(values.link_id), false);
  assert.equal(serialized.includes("Mozilla"), false);

  assert.equal((await worker.fetch(new Request(`${PUBLIC}/api/actions/action_bad/events`, { headers: { authorization: `Bearer ${DASHBOARD_SECRET}` } }), env)).status, 404);
  // The plain /api/actions/<id> route must reject an id containing a slash
  // rather than passing it through to requireOpaqueId as one opaque string.
  assert.equal((await worker.fetch(new Request(`${PUBLIC}/api/actions/${values.action_id}/something-else`, { headers: { authorization: `Bearer ${DASHBOARD_SECRET}` } }), env)).status, 404);
  assert.equal((await worker.fetch(new Request(url, { method: "POST", headers: { authorization: `Bearer ${DASHBOARD_SECRET}` } }), env)).status, 405);

  const baseNow = Date.now();
  for (let index = 0; index < 205; index += 1) {
    db.events.push({
      event_id: `event_extra_${index}`,
      link_id: values.link_id,
      action_id: values.action_id,
      occurred_at: new Date(baseNow - index * 1000).toISOString().replace(/\.\d{3}Z$/u, "Z"),
      hour_bucket: "2026-01-01T00:00:00Z",
      event_type: "click",
      source_kind: "direct_browser",
      user_agent_family: "browser",
      network_class: "unknown",
      machine_cadence: 0,
      fanout_count: 1,
      event_fingerprint_hash: `sha256:${String(index).padStart(64, "0")}`,
      classification: "unknown",
      reason_codes_json: JSON.stringify(["insufficient_human_or_automation_evidence"]),
      created_at: new Date(baseNow).toISOString().replace(/\.\d{3}Z$/u, "Z"),
    });
  }
  const capped = await worker.fetch(new Request(url, { headers: { authorization: `Bearer ${DASHBOARD_SECRET}` } }), env);
  const cappedPayload = await capped.json();
  assert.equal(cappedPayload.events.length, 200);
  assert.equal(cappedPayload.truncated, true);
});

test("dashboard and action summaries include deck_view in event_type_counts, initialized to zero", async () => {
  const db = new FakeD1();
  const access = await accessEnv();
  const assets = new FakeAssets();
  const env = environment(db, access.jwks, undefined, assets);
  const values = await p39(ids("25"));
  const minted = await mint(env, values);
  await activate(env, values);

  const dashboardBefore = await worker.fetch(new Request(`${PUBLIC}/api/dashboard`, { headers: { "cf-access-jwt-assertion": await accessToken(access.pair) } }), env);
  const beforePayload = await dashboardBefore.json();
  assert.deepEqual(beforePayload.event_type_counts.deck_view, { automation_likely: 0, unknown: 0, human_likely: 0 });

  await worker.fetch(new Request(minted.deck_url, { headers: { "user-agent": "Mozilla/5.0 Chrome/120.0", "cf-connecting-ip": "198.51.100.9" } }), env);

  const dashboardAfter = await worker.fetch(new Request(`${PUBLIC}/api/dashboard`, { headers: { "cf-access-jwt-assertion": await accessToken(access.pair) } }), env);
  const afterPayload = await dashboardAfter.json();
  const deckTotal = afterPayload.event_type_counts.deck_view.human_likely + afterPayload.event_type_counts.deck_view.unknown + afterPayload.event_type_counts.deck_view.automation_likely;
  assert.equal(deckTotal, 1);

  const actionResponse = await worker.fetch(new Request(`${PUBLIC}/api/actions/${values.action_id}`, { headers: { "cf-access-jwt-assertion": await accessToken(access.pair) } }), env);
  const actionPayload = await actionResponse.json();
  assert.deepEqual(Object.keys(actionPayload.event_type_counts.deck_view).sort(), ["automation_likely", "human_likely", "unknown"]);
  const actionDeckTotal = actionPayload.event_type_counts.deck_view.human_likely + actionPayload.event_type_counts.deck_view.unknown + actionPayload.event_type_counts.deck_view.automation_likely;
  assert.equal(actionDeckTotal, 1);
});

test("staging config contains the retention cleanup cron", async () => {
  const config = await readFile(new URL("../wrangler.toml.example", import.meta.url), "utf8");
  assert.match(config, /\[triggers\][\s\S]*crons\s*=\s*\["0 \* \* \* \*"\]/u);
});

test("staging migration contains only pseudonymized dashboard rate fields", async () => {
  const migration = await readFile(new URL("../migrations/0002_dashboard_login_rate.sql", import.meta.url), "utf8");
  assert.match(migration, /dashboard_login_rate/u);
  assert.match(migration, /rate_key/u);
  assert.doesNotMatch(migration, /\b(?:ip|recipient|secret|session)_?\w*\s+TEXT\b/iu);
});

test("deck view migration rebuilds events with the three-value CHECK and recreates both events indexes", async () => {
  const migration = await readFile(new URL("../migrations/0003_deck_view_events.sql", import.meta.url), "utf8");
  assert.match(migration, /event_type TEXT NOT NULL CHECK \(event_type IN \('open', 'click', 'deck_view'\)\)/u);
  assert.match(migration, /INSERT INTO events_new SELECT \* FROM events/u);
  assert.match(migration, /DROP TABLE events;/u);
  assert.match(migration, /ALTER TABLE events_new RENAME TO events;/u);
  assert.match(migration, /CREATE INDEX IF NOT EXISTS idx_events_action_hour\s*\n\s*ON events \(action_id, hour_bucket\);/u);
  assert.match(migration, /CREATE INDEX IF NOT EXISTS idx_events_occurred_at\s*\n\s*ON events \(occurred_at\);/u);
});
