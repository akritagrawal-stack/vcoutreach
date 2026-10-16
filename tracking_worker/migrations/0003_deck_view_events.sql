-- Adds deck_view to events.event_type so a signed pitch-deck view can be
-- recorded alongside opens and clicks. SQLite cannot ALTER a CHECK
-- constraint, so this rebuilds the events table with an identical shape and
-- a widened event_type CHECK, preserving every existing row, then recreates
-- both events indexes from 0001 exactly.
--
-- Run this once against the production D1 database before deploying the
-- Worker code that writes event_type = 'deck_view'. It is safe to run more
-- than once (the rebuilt table keeps the same rows and shape each time), but
-- it is not designed to be run concurrently with live event writes.

DROP TABLE IF EXISTS events_new;

CREATE TABLE events_new (
  event_id TEXT PRIMARY KEY,
  link_id TEXT NOT NULL REFERENCES links(link_id),
  action_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  hour_bucket TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('open', 'click', 'deck_view')),
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

INSERT INTO events_new SELECT * FROM events;

DROP TABLE events;

ALTER TABLE events_new RENAME TO events;

CREATE INDEX IF NOT EXISTS idx_events_action_hour
  ON events (action_id, hour_bucket);
CREATE INDEX IF NOT EXISTS idx_events_occurred_at
  ON events (occurred_at);
