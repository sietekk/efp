-- 0009_state_adapters_broken: state_adapters.mode also allows 'broken' (build 08).
--
-- A state AG module whose live smoke test fails is reported as 'broken' in the coverage matrix,
-- not silently skipped. state_adapters is not a protected table, so it is rebuilt (SQLite cannot
-- alter a CHECK in place). Existing rows are copied unchanged.

CREATE TABLE state_adapters_new (
  state          TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('ag','docket')),
  portal         TEXT,
  adapter_module TEXT,
  mode           TEXT NOT NULL CHECK (mode IN ('automated','assisted','manual','none','broken')),
  reason         TEXT,
  last_verified  TEXT,
  PRIMARY KEY (state, kind)
);

INSERT INTO state_adapters_new (state, kind, portal, adapter_module, mode, reason, last_verified)
SELECT state, kind, portal, adapter_module, mode, reason, last_verified FROM state_adapters;

DROP TABLE state_adapters;
ALTER TABLE state_adapters_new RENAME TO state_adapters;
