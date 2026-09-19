CREATE TABLE IF NOT EXISTS customers (
  id UUID PRIMARY KEY,
  company_name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  domain TEXT NOT NULL UNIQUE,
  stack_name TEXT NOT NULL UNIQUE,
  stack_id BIGINT,
  endpoint_id BIGINT NOT NULL,
  subnet TEXT NOT NULL UNIQUE,
  admin_email TEXT NOT NULL,
  secrets_encrypted BYTEA NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  last_error TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deployed_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS customers_status_idx ON customers(status);
