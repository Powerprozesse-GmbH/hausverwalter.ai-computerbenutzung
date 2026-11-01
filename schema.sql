-- Local metering (SQLite) for the internal providers (bedrock, vertex, azure, tensorx).
-- Customer mode (HV_PROVIDER=hausverwalter) stores nothing here: the hausverwalter.ai server bills.

CREATE TABLE IF NOT EXISTS tenants (
  tenant_id              TEXT PRIMARY KEY,
  name                   TEXT NOT NULL,
  inference_profile_arn  TEXT NOT NULL UNIQUE,  -- application inference profile, tagged TenantID
  budget_micro_usd       INTEGER NOT NULL,      -- 1 USD = 1_000_000
  created_at             INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
  id                           TEXT PRIMARY KEY,
  tenant_id                    TEXT NOT NULL REFERENCES tenants(tenant_id),
  task_id                      TEXT NOT NULL,
  created_at                   INTEGER NOT NULL,
  model                        TEXT NOT NULL,
  aws_request_id               TEXT,              -- joins invocation logs / CUR
  input_tokens                 INTEGER NOT NULL,
  output_tokens                INTEGER NOT NULL,
  cache_creation_input_tokens  INTEGER NOT NULL,
  cache_read_input_tokens      INTEGER NOT NULL,
  cost_micro_usd               INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS usage_events_by_tenant ON usage_events (tenant_id, created_at);
