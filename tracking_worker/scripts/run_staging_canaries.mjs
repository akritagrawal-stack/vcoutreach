#!/usr/bin/env node

/*
 * Staging-only remote probe for the isolated hosted tracker. It creates no
 * email and prints no secret, signature, recipient, destination, or payload.
 * Required secrets come only from the process environment after being written
 * to Cloudflare with Wrangler secret put.
 */
import {
  canonicalJson,
  validateHttpsOrigin,
  expectedP39LinkHash,
  hmac,
  isoNow,
  sha256,
  signedLinkUrl,
} from "../src/logic.mjs";
import { execFileSync } from "node:child_process";

const origin = validateHttpsOrigin(process.env.TRACKING_WORKER_URL);
const trackingKey = process.env.STAGING_TRACKING_SIGNING_KEY;
const internalKey = process.env.STAGING_INTERNAL_API_KEY;
const activationKey = process.env.STAGING_ACTIVATION_SIGNING_KEY;
const dashboardSecret = process.env.STAGING_DASHBOARD_ACCESS_SECRET || (process.env.PUBLISH_DASHBOARD_KEYCHAIN === "1"
  ? execFileSync("/usr/bin/security", ["find-generic-password", "-s", "outreach-signal.tracking", "-a", "dashboard-access-secret", "-w"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim()
  : undefined);
const destinationKey = "publik_home";

if (!origin || !trackingKey || !internalKey || !activationKey || !dashboardSecret) {
  throw new Error("staging origin and four staging secrets are required");
}

function randomId(prefix) {
  return `${prefix}_${Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

function afterSeconds(seconds) {
  return new Date(Date.now() + seconds * 1000).toISOString().replace(/\.\d{3}Z$/u, "Z");
}

async function request(path, options = {}) {
  return fetch(`${origin}${path}`, { redirect: "manual", ...options });
}

async function requireStatus(label, response, expected) {
  if (response.status !== expected) throw new Error(`${label} expected ${expected}, received ${response.status}`);
  return { label, status: response.status };
}

async function p18Receipt(actionId) {
  const receipt = {
    schema_version: "1",
    receipt_kind: "publik_gmail_test_provider_result",
    action_id: actionId,
    manifest_hash: `sha256:${"a".repeat(64)}`,
    local_execution_receipt_hash: `sha256:${"b".repeat(64)}`,
    request_hash: `sha256:${"c".repeat(64)}`,
    message_sha256: `sha256:${"d".repeat(64)}`,
    message_id_sha256: `sha256:${"e".repeat(64)}`,
    outcome: "success",
    executed_at: isoNow(),
    provider_message_id_hash: `sha256:${"f".repeat(64)}`,
    retry_disposition: "no_retry_needed",
    automatic_retry_authorized: false,
    network_accessed: true,
    live_recipient_used: false,
    authority: {
      automation_promotion_authorized: false,
      arbitrary_recipient_authorized: false,
      delivery_authorized: true,
      external_actions_authorized: true,
      external_use_authorized: false,
      live_delivery_authorized: false,
      network_access_authorized: true,
      provider_send_authorized: true,
      submission_authorized: false,
      test_delivery_authorized: true,
      tracking_authorized: false,
    },
  };
  receipt.receipt_hash = await sha256(canonicalJson(receipt));
  return receipt;
}

async function mint(expiresAt) {
  const link = {
    campaign_id: randomId("campaign"),
    action_id: randomId("action"),
    link_id: randomId("link"),
    destination_key: destinationKey,
    expires_at: expiresAt,
  };
  const linkHash = await expectedP39LinkHash(link, origin, trackingKey);
  const response = await request("/internal/links", {
    method: "POST",
    headers: { "content-type": "application/json", "x-publik-internal-key": internalKey },
    body: JSON.stringify({ ...link, link_hash: linkHash }),
  });
  await requireStatus("mint", response, 201);
  const urls = await response.json();
  if (urls.redirect_url !== await signedLinkUrl(link, origin, trackingKey)) throw new Error("mint returned an unexpected redirect signature");
  return { link: { ...link, link_hash: linkHash }, urls };
}

async function activate(link) {
  const receipt = await p18Receipt(link.action_id);
  const claims = {
    accepted_at: receipt.executed_at,
    action_id: link.action_id,
    link_hash: link.link_hash,
    link_id: link.link_id,
    receipt_hash: receipt.receipt_hash,
    receipt_id: `receipt_${receipt.receipt_hash.slice("sha256:".length, "sha256:".length + 32)}`,
  };
  const response = await request("/internal/activate", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-publik-internal-key": internalKey,
      "x-publik-activation-mac": await hmac(claims, activationKey),
    },
    body: JSON.stringify({ link_id: link.link_id, receipt }),
  });
  return requireStatus("activate", response, 201);
}

const checks = [];
const active = await mint(afterSeconds(120));
checks.push(await activate(active.link));
checks.push(await requireStatus("unknown", await request(`/r/${randomId("link")}?s=${"x".repeat(43)}`), 404));
checks.push(await requireStatus("tampered", await request(`${active.urls.redirect_url.slice(origin.length)}x`), 404));
checks.push(await requireStatus("head", await request(active.urls.open_url.slice(origin.length), { method: "HEAD" }), 204));
checks.push(await requireStatus("scanner_open", await request(active.urls.open_url.slice(origin.length), { headers: { "user-agent": "GoogleImageProxy" } }), 200));
checks.push(await requireStatus("early_click", await request(active.urls.redirect_url.slice(origin.length), { headers: { "user-agent": "Mozilla/5.0 Chrome/120.0" } }), 302));
checks.push(await requireStatus("dedup_first", await request(active.urls.open_url.slice(origin.length), { headers: { "user-agent": "GoogleImageProxy" } }), 200));
checks.push(await requireStatus("dedup_second", await request(active.urls.open_url.slice(origin.length), { headers: { "user-agent": "GoogleImageProxy" } }), 200));
checks.push(await requireStatus("unsubscribe_get", await request(active.urls.unsubscribe_url.slice(origin.length)), 405));
checks.push(await requireStatus("unsubscribe_post", await request(active.urls.unsubscribe_url.slice(origin.length), {
  method: "POST",
  headers: { "content-type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams({ "List-Unsubscribe": "One-Click" }),
}), 204));
checks.push(await requireStatus("dashboard_fail_closed", await request("/api/dashboard"), 401));
checks.push(await requireStatus("dashboard_bad_bearer", await request("/api/dashboard", { headers: { authorization: "Bearer invalid" } }), 401));
const dashboard = await request("/api/dashboard", { headers: { authorization: `Bearer ${dashboardSecret}` } });
checks.push(await requireStatus("dashboard_bearer", dashboard, 200));
if (dashboard.headers.get("access-control-allow-origin") !== null) throw new Error("dashboard unexpectedly enables CORS");
const aggregate = await dashboard.json();
if (JSON.stringify(aggregate).includes(dashboardSecret)) throw new Error("dashboard response contains access secret");
checks.push({ label: "dashboard_aggregate_contract", status: 200, aggregate_only: aggregate.classification === "aggregate_only", opens_and_clicks_not_people: aggregate.interpretation?.opens_and_clicks_are_not_confirmed_people === true });
const login = await request("/dashboard/login", {
  method: "POST",
  headers: { "content-type": "application/x-www-form-urlencoded", origin },
  body: new URLSearchParams({ secret: dashboardSecret }),
});
checks.push(await requireStatus("dashboard_login", login, 303));
const sessionCookie = login.headers.get("set-cookie");
if (!sessionCookie || !/^__Host-publik_dashboard=/u.test(sessionCookie) || !/HttpOnly; Secure; SameSite=Strict$/u.test(sessionCookie) || sessionCookie.includes(dashboardSecret)) throw new Error("dashboard session cookie contract failed");
checks.push(await requireStatus("dashboard_session", await request("/dashboard", { headers: { cookie: sessionCookie.split(";", 1)[0] } }), 200));
checks.push(await requireStatus("dashboard_logout", await request("/dashboard/logout", { method: "POST", headers: { cookie: sessionCookie.split(";", 1)[0] } }), 204));
checks.push(await requireStatus("dashboard_cross_origin_login", await request("/dashboard/login", {
  method: "POST",
  headers: { "content-type": "application/x-www-form-urlencoded", origin: "https://attacker.example.test" },
  body: new URLSearchParams({ secret: dashboardSecret }),
}), 404));

const expiring = await mint(afterSeconds(3));
checks.push(await activate(expiring.link));
await new Promise((resolve) => setTimeout(resolve, 4000));
checks.push(await requireStatus("expired", await request(expiring.urls.redirect_url.slice(origin.length)), 404));

const receipt = {
  schema_version: "1",
  receipt_kind: "publik_hosted_tracking_staging_canary",
  outcome: "success",
  checks,
  dashboard_access_state: "staging_worker_secret_fallback_verified",
  identifiers_redacted: true,
};
receipt.receipt_hash = await sha256(canonicalJson(receipt));
process.stdout.write(`${JSON.stringify(receipt)}\n`);
