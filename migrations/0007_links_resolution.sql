-- 0007_links_resolution: the links table, widened for the link-resolution stage (build 06).
--
-- links is not a protected table, so it is rebuilt (SQLite cannot alter a CHECK in place).
--   * expansion_method also allows 'bitly_info' (Bitly's + page) and 'commoncrawl'.
--   * note: why a link has the status it has ('no_url', 'robots disallowed', ...).
--   * UNIQUE (row_id, url_raw): a stage run can be repeated without duplicating links.
-- Existing rows are copied with their link_id.

CREATE TABLE links_new (
  link_id          INTEGER PRIMARY KEY,
  row_id           INTEGER NOT NULL REFERENCES rows(row_id),
  url_raw          TEXT NOT NULL,                 -- as it appears in the Source cell ('' or the
                                                  -- cell text for a row with no URL, note 'no_url')
  url_expanded     TEXT,
  expansion_method TEXT CHECK (expansion_method IN
                   ('live','bitly_info','wayback_short','wayback_target','commoncrawl','unresolved')),
  status           TEXT NOT NULL CHECK (status IN ('live','archived','dead','unresolved')),
  capture_id       INTEGER REFERENCES captures(capture_id),
  resolved_at      TEXT,
  note             TEXT,
  UNIQUE (row_id, url_raw)
);

INSERT INTO links_new (link_id, row_id, url_raw, url_expanded, expansion_method, status,
                       capture_id, resolved_at)
SELECT link_id, row_id, url_raw, url_expanded, expansion_method, status, capture_id, resolved_at
FROM links;

DROP TABLE links;
ALTER TABLE links_new RENAME TO links;

CREATE INDEX idx_links_row     ON links(row_id);
CREATE INDEX idx_links_capture ON links(capture_id);
