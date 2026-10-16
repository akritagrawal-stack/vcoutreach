-- Staging-only dashboard-login throttle. rate_key is an HMAC pseudonym bound
-- to a five-minute window, never a raw IP, dashboard secret, or session value.
CREATE TABLE IF NOT EXISTS dashboard_login_rate (
  rate_key TEXT PRIMARY KEY,
  window_start TEXT NOT NULL,
  attempts INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 100),
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dashboard_login_rate_updated_at
  ON dashboard_login_rate (updated_at);
