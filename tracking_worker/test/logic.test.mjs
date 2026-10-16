import assert from "node:assert/strict";
import test from "node:test";
import {
  canonicalJson,
  classifyRequest,
  configuredSecret,
  destinationMap,
  expectedP39LinkHash,
  exactOriginList,
  hmac,
  linkClaims,
  requireUtc,
  sha256,
  signedLinkUrl,
  signedOpenUrl,
  signedUnsubscribeUrl,
  validateTrackingBridgeActivation,
  validateManualLiveReceipt,
  validateP18Receipt,
  verifyRouteSignature,
} from "../src/logic.mjs";

const SECRET = "p39-test-signing-secret-must-be-at-least-32-bytes";
const ORIGIN = "https://track.example.test";
const LINK = {
  campaign_id: "campaign_00000000000000000000000000000001",
  action_id: "action_00000000000000000000000000000001",
  link_id: "link_00000000000000000000000000000001",
  destination_key: "publik_home",
  expires_at: "2099-01-01T00:00:00Z",
};

async function p18Receipt(actionId = LINK.action_id, manifestHash = "sha256:" + "a".repeat(64), localReceiptHash = "sha256:" + "b".repeat(64)) {
  const receipt = {
    schema_version: "1",
    receipt_kind: "publik_gmail_test_provider_result",
    action_id: actionId,
    manifest_hash: manifestHash,
    local_execution_receipt_hash: localReceiptHash,
    request_hash: "sha256:" + "c".repeat(64),
    message_sha256: "sha256:" + "d".repeat(64),
    message_id_sha256: "sha256:" + "e".repeat(64),
    outcome: "success",
    executed_at: "2026-07-30T23:00:00Z",
    provider_message_id_hash: "sha256:" + "f".repeat(64),
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

async function manualLiveReceipt(actionId = LINK.action_id) {
  const receipt = {
    schema_version: "1",
    receipt_kind: "publik_gmail_manual_live_provider_result",
    action_id: actionId,
    manifest_hash: "sha256:" + "a".repeat(64),
    local_execution_receipt_hash: "sha256:" + "b".repeat(64),
    request_hash: "sha256:" + "c".repeat(64),
    message_sha256: "sha256:" + "d".repeat(64),
    message_id_sha256: "sha256:" + "e".repeat(64),
    outcome: "success",
    executed_at: "2026-08-03T20:00:00Z",
    provider_message_id_hash: "sha256:" + "f".repeat(64),
    retry_disposition: "no_retry_needed",
    automatic_retry_authorized: false,
    network_accessed: true,
    live_recipient_used: true,
    authority: {
      automation_promotion_authorized: false,
      arbitrary_recipient_authorized: false,
      delivery_authorized: true,
      external_actions_authorized: true,
      external_use_authorized: true,
      live_delivery_authorized: true,
      network_access_authorized: true,
      provider_send_authorized: true,
      submission_authorized: false,
      test_delivery_authorized: false,
      tracking_authorized: true,
    },
  };
  receipt.receipt_hash = await sha256(canonicalJson(receipt));
  return receipt;
}

async function p17BridgeActivation() {
  const actionId = "test_action_4706929fdf706388db0d5b92";
  const manifest = {
    schema_version: "1", manifest_kind: "publik_local_test_action", action_id: actionId,
    created_at: "2026-07-30T22:00:00Z", candidate_id: "p33_synthetic_investor",
    contact_record_hash: "sha256:" + "1".repeat(64), vault_record_hash: "sha256:" + "2".repeat(64), source_provenance_hash: "sha256:" + "3".repeat(64), verified_at: "2026-07-30T21:00:00Z", expires_at: "2026-08-02T00:00:00Z", prepare_contact_lookup_receipt_hash: "sha256:" + "4".repeat(64), recipient_email_hash: "sha256:" + "5".repeat(64), recipient_person_firm_hash: "sha256:" + "6".repeat(64), test_recipient_email_hash: "sha256:cfe3b343e212125488fd742d586c9d33b8b66def5a83a1691d7180ee41f404c5", research_receipt_hash: "sha256:" + "8".repeat(64), draft_hash: "sha256:" + "9".repeat(64), subject_sha256: "sha256:" + "a".repeat(64), body_sha256: "sha256:" + "b".repeat(64), deck_sha256: "sha256:" + "c".repeat(64), route_type: "cold_email", suppression_export_sha256: "sha256:" + "d".repeat(64), suppression_records_sha256: "sha256:" + "e".repeat(64), suppression_record_count: 175, suppression_checks: ["prepare", "pre_execute"], execution_mode: "local_test_only", delivery_target: "approved_test_inbox", status: "ready_for_local_test_execution", network_accessed: false, live_recipient_authorized: false, local_test_execution_authorized: true,
    authority: { automation_promotion_authorized: false, delivery_authorized: false, external_actions_authorized: false, external_use_authorized: false, live_delivery_authorized: false, network_access_authorized: false, submission_authorized: false, tracking_authorized: false },
  };
  manifest.manifest_hash = await sha256(canonicalJson(manifest));
  const provider = await p18Receipt(actionId, manifest.manifest_hash, "sha256:" + "f".repeat(64));
  const inbox = {
    schema_version: "1", receipt_kind: "publik_gmail_test_inbox_arrival", action_id: actionId,
    manifest_hash: manifest.manifest_hash, local_execution_receipt_hash: provider.local_execution_receipt_hash,
    provider_result_receipt_hash: provider.receipt_hash, provider_message_id_hash: provider.provider_message_id_hash,
    gmail_message_id_hash: "sha256:" + "1".repeat(64), message_id_hash: provider.message_id_sha256,
    subject_sha256: "sha256:" + "2".repeat(64), verified_at: "2026-07-30T23:01:00Z",
    arrival_status: "confirmed_inbox", inbox_arrival_confirmed: true, attachment_presence_confirmed: true,
    network_accessed: true, gmail_send_performed: false,
    authority: { delivery_authorized: false, gmail_send_authorized: false, gmail_read_authorized: true, live_delivery_authorized: false },
  };
  inbox.receipt_hash = await sha256(canonicalJson(inbox));
  const source = { source_action_id: actionId, source_manifest_hash: manifest.manifest_hash, source_provider_receipt_hash: provider.receipt_hash, source_inbox_receipt_hash: inbox.receipt_hash };
  const trackingActionId = `action_${(await sha256(canonicalJson(source))).slice("sha256:".length, "sha256:".length + 32)}`;
  const bridge = { schema_version: "1", artifact_kind: "publik_p17_to_p39_tracking_action_bridge", ...source, tracking_action_id: trackingActionId, provider_accepted_at: provider.executed_at, created_at: "2026-07-31T00:00:00Z", expires_at: "2026-08-02T00:00:00Z", authority: { automation_promotion_authorized: false, delivery_authorized: false, external_actions_authorized: false, external_use_authorized: false, live_delivery_authorized: false, tracking_authorized: false } };
  bridge.bridge_hash = await sha256(canonicalJson(bridge));
  const activation = { schema_version: "1", receipt_kind: "publik_p17_to_p39_tracking_activation", action_id: trackingActionId, source_action_id: actionId, source_manifest_hash: manifest.manifest_hash, source_manifest: manifest, source_provider_receipt: provider, source_inbox_receipt: inbox, bridge, executed_at: provider.executed_at, expires_at: bridge.expires_at, authority: bridge.authority };
  activation.receipt_hash = await sha256(canonicalJson(activation));
  return activation;
}

test("P39 link claims are byte-compatible with the documented HMAC vector", async () => {
  assert.equal(canonicalJson(linkClaims(LINK, ORIGIN)), '{"action_id":"action_00000000000000000000000000000001","campaign_id":"campaign_00000000000000000000000000000001","destination_key":"publik_home","expires_at":"2099-01-01T00:00:00Z","link_id":"link_00000000000000000000000000000001","origin":"https://track.example.test"}');
  assert.equal(await hmac(linkClaims(LINK, ORIGIN), SECRET), "5YfuWe1tmSQhwTK_5kxDoCVp67guywANaFTHTD21plk");
  assert.equal(await expectedP39LinkHash(LINK, ORIGIN, SECRET), "sha256:07212580a11bc6b0b4b59ba4ea2b9e8c8394e542b1f2e98f5e1441a81c6bb326");
  const redirect = await signedLinkUrl(LINK, ORIGIN, SECRET);
  const open = await signedOpenUrl(LINK, ORIGIN, SECRET);
  const unsubscribe = await signedUnsubscribeUrl(LINK, ORIGIN, SECRET);
  assert.match(redirect, /^https:\/\/track\.example\.test\/r\/link_[0-9a-f]{32}\?s=[A-Za-z0-9_-]{43}$/u);
  assert.notEqual(redirect, unsubscribe);
  assert.notEqual(redirect, open);
  assert.equal(await verifyRouteSignature(LINK, ORIGIN, new URL(redirect).searchParams.get("s"), "redirect", SECRET), true);
  assert.equal(await verifyRouteSignature(LINK, ORIGIN, new URL(redirect).searchParams.get("s"), "unsubscribe", SECRET), false);
  assert.equal(await verifyRouteSignature(LINK, ORIGIN, new URL(open).searchParams.get("s"), "open", SECRET), true);
});

test("P18 activation receipt requires an exact canonical receipt hash", async () => {
  const receipt = await p18Receipt();
  await assert.doesNotReject(validateP18Receipt(receipt));
  const tampered = { ...receipt, outcome: "failure" };
  await assert.rejects(validateP18Receipt(tampered), /receipt is not a successful|receipt hash/u);
  const rehashed = { ...receipt, executed_at: "2026-07-30T23:01:00Z" };
  await assert.rejects(validateP18Receipt(rehashed), /receipt hash does not match content/u);
});

test("manual live activation receipt is exact, scoped, and independently validated", async () => {
  const receipt = await manualLiveReceipt();
  await assert.doesNotReject(validateManualLiveReceipt(receipt));
  const testRouted = { ...receipt, live_recipient_used: false };
  const unsignedTestRouted = { ...testRouted };
  delete unsignedTestRouted.receipt_hash;
  testRouted.receipt_hash = await sha256(canonicalJson(unsignedTestRouted));
  await assert.rejects(validateManualLiveReceipt(testRouted), /manual live|hash/u);
  const widened = structuredClone(receipt);
  widened.authority.arbitrary_recipient_authorized = true;
  const unsigned = { ...widened };
  delete unsigned.receipt_hash;
  widened.receipt_hash = await sha256(canonicalJson(unsigned));
  await assert.rejects(validateManualLiveReceipt(widened), /manual live/u);
});

test("P17 tracking bridge derives one opaque P39 action from bound P17, P18, and P36 receipts", async () => {
  const activation = await p17BridgeActivation();
  await assert.doesNotReject(validateTrackingBridgeActivation(activation));
  assert.match(activation.action_id, /^action_[0-9a-f]{32}$/u);
  const tampered = structuredClone(activation);
  tampered.source_inbox_receipt.provider_result_receipt_hash = "sha256:" + "0".repeat(64);
  await assert.rejects(validateTrackingBridgeActivation(tampered), /binding|hash/u);
});

test("invalid normalized dates fail closed", () => {
  assert.throws(() => requireUtc("2026-02-30T00:00:00Z"), /invalid UTC/u);
  assert.throws(() => requireUtc("2026-07-30T00:00:00+00:00"), /invalid UTC/u);
  assert.equal(requireUtc("2026-07-30T00:00:00Z"), "2026-07-30T00:00:00Z");
});

test("internal keys fail closed when either configured or presented secret is short", () => {
  assert.equal(configuredSecret(""), false);
  assert.equal(configuredSecret("too-short"), false);
  assert.equal(configuredSecret(SECRET), true);
});

test("classification remains scanner-conservative and never promotes opens", () => {
  const scanner = classifyRequest({ eventType: "click", occurredAt: "2026-07-30T23:01:00Z", acceptedAt: "2026-07-30T23:00:00Z", userAgent: "Mozilla safelinks", cf: { asOrganization: "Security Scanner" } });
  assert.equal(scanner.classification, "automation_likely");
  assert.deepEqual(scanner.reasonCodes, ["automation_network_class", "automation_user_agent_family", "post_send_latency_under_180_seconds", "security_scanner"]);
  const open = classifyRequest({ eventType: "open", occurredAt: "2026-07-30T23:04:00Z", acceptedAt: "2026-07-30T23:00:00Z", userAgent: "Mozilla/5.0 Chrome/120.0", cf: { asOrganization: "Comcast Cable" } });
  assert.equal(open.classification, "unknown");
  const human = classifyRequest({ eventType: "click", occurredAt: "2026-07-30T23:04:00Z", acceptedAt: "2026-07-30T23:00:00Z", userAgent: "Mozilla/5.0 Chrome/120.0", cf: { asOrganization: "Comcast Cable" } });
  assert.equal(human.classification, "human_likely");
});

test("deck view classification treats latency, scanners, and cadence like a click but does not require a residential or mobile network", () => {
  const fast = classifyRequest({ eventType: "deck_view", occurredAt: "2026-07-30T23:01:00Z", acceptedAt: "2026-07-30T23:00:00Z", userAgent: "Mozilla/5.0 Chrome/120.0", cf: { asOrganization: "Comcast Cable" } });
  assert.equal(fast.classification, "automation_likely");
  assert.ok(fast.reasonCodes.includes("post_send_latency_under_180_seconds"));

  const scanner = classifyRequest({ eventType: "deck_view", occurredAt: "2026-07-30T23:10:00Z", acceptedAt: "2026-07-30T23:00:00Z", userAgent: "Mozilla safelinks", cf: { asOrganization: "Comcast Cable" } });
  assert.equal(scanner.classification, "automation_likely");
  assert.ok(scanner.reasonCodes.includes("security_scanner"));

  const hosting = classifyRequest({ eventType: "deck_view", occurredAt: "2026-07-30T23:10:00Z", acceptedAt: "2026-07-30T23:00:00Z", userAgent: "Mozilla/5.0 Chrome/120.0", cf: { asOrganization: "Amazon Web Services" } });
  assert.equal(hosting.classification, "automation_likely");
  assert.ok(hosting.reasonCodes.includes("automation_network_class"));

  const cadence = classifyRequest({ eventType: "deck_view", occurredAt: "2026-07-30T23:10:00Z", acceptedAt: "2026-07-30T23:00:00Z", userAgent: "Mozilla/5.0 Chrome/120.0", cf: { asOrganization: "Comcast Cable" }, recentCount: 5 });
  assert.equal(cadence.classification, "automation_likely");
  assert.ok(cadence.reasonCodes.includes("machine_cadence"));

  const humanUnknownNetwork = classifyRequest({ eventType: "deck_view", occurredAt: "2026-07-30T23:10:00Z", acceptedAt: "2026-07-30T23:00:00Z", userAgent: "Mozilla/5.0 Chrome/120.0", cf: {} });
  assert.equal(humanUnknownNetwork.networkClass, "unknown");
  assert.equal(humanUnknownNetwork.classification, "human_likely");
  assert.deepEqual(humanUnknownNetwork.reasonCodes, ["plausible_browser_followthrough"]);

  const stillNoScanner = classifyRequest({ eventType: "deck_view", occurredAt: "2026-07-30T23:10:00Z", acceptedAt: "2026-07-30T23:00:00Z", userAgent: "curl/8.0", cf: {} });
  assert.equal(stillNoScanner.classification, "automation_likely");
});

test("destination resolution is server-side and exact allow-list only", () => {
  const allowed = exactOriginList("https://publik.example.test");
  const destinations = destinationMap(JSON.stringify({ publik_home: "https://publik.example.test/demo" }), allowed);
  assert.equal(destinations.get("publik_home"), "https://publik.example.test/demo");
  assert.throws(() => destinationMap(JSON.stringify({ publik_home: "https://other.example.test/demo" }), allowed), /allow-list/u);
  assert.throws(() => destinationMap(JSON.stringify({ publik_home: "https://publik.example.test/demo?key=secret" }), allowed), /allow-list/u);
});
