-- Staging-only D1 schema. It retains opaque IDs, category classifications, and
-- hashed fingerprints only. It never stores destination URLs, recipients, raw
-- email, raw IP, full user agents, headers, or query-string signatures.

CREATE TABLE IF NOT EXISTS links (
  link_id TEXT PRIMARY KEY CHECK (link_id GLOB 'link_*'),
  campaign_id TEXT NOT NULL CHECK (campaign_id GLOB 'campaign_*'),
  action_id TEXT NOT NULL CHECK (action_id GLOB 'action_*'),
  destination_key TEXT NOT NULL,
  link_hash TEXT NOT NULL CHECK (link_hash GLOB 'sha256:*'),
  expires_at TEXT NOT NULL,
  activated_at TEXT,
  receipt_hash TEXT,
  created_at TEXT NOT NULL,
  CHECK (length(destination_key) BETWEEN 3 AND 128)
);

CREATE INDEX IF NOT EXISTS idx_links_activation_expiry
  ON links (activated_at, expires_at);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  link_id TEXT NOT NULL REFERENCES links(link_id),
  action_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  hour_bucket TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('open', 'click')),
  source_kind TEXT NOT NULL CHECK (source_kind IN ('direct_browser', 'gmail_proxy', 'safe_links', 'proofpoint', 'generic_scanner', 'unknown')),
  user_agent_family TEXT NOT NULL CHECK (user_agent_family IN ('browser', 'mail_proxy', 'security_scanner', 'link_rewriter', 'unknown')),
  network_class TEXT NOT NULL CHECK (network_class IN ('residential', 'mobile', 'hosting', 'security', 'unknown')),
  machine_cadence INTEGER NOT NULL CHECK (machine_cadence IN (0, 1)),
  fanout_count INTEGER NOT NULL CHECK (fanout_count BETWEEN 1 AND 50),
  event_fingerprint_hash TEXT NOT NULL UNIQUE,
  classification TEXT NOT NULL CHECK (classification IN ('automation_likely', 'unknown', 'human_likely')),
  reason_codes_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_action_hour
  ON events (action_id, hour_bucket);
CREATE INDEX IF NOT EXISTS idx_events_occurred_at
  ON events (occurred_at);

CREATE TABLE IF NOT EXISTS unsubscribe_denials (
  action_id TEXT PRIMARY KEY,
  link_id TEXT NOT NULL REFERENCES links(link_id),
  received_at TEXT NOT NULL,
  idempotency_fingerprint_hash TEXT NOT NULL UNIQUE,
  deny_mac TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- A downstream authorized send gate consumes this opaque denial record and maps
-- action_id to its private recipient record. The tracker itself has no recipient.
CREATE TABLE IF NOT EXISTS suppression_outbox (
  action_id TEXT PRIMARY KEY,
  denial_fingerprint_hash TEXT NOT NULL UNIQUE,
  deny_mac TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_at TEXT
);
