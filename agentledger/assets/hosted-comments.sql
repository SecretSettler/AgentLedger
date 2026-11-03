-- Apply through the host's migration mechanism, not inside a request handler.
CREATE TABLE review_comments (
  id TEXT PRIMARY KEY NOT NULL,
  report TEXT NOT NULL,
  version INTEGER NOT NULL,
  owner TEXT NOT NULL,
  row_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  github_updated TEXT NOT NULL DEFAULT '',
  github_checked TEXT NOT NULL DEFAULT ''
);
CREATE INDEX review_comments_report_version ON review_comments(report, version);
