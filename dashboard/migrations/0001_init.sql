-- Single-row switch read by the Oracle runner before every scheduled run.
CREATE TABLE control (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  stopped INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  actor_uid INTEGER,
  actor_name TEXT,
  updated_at TEXT NOT NULL,
  runner_seen_at TEXT,
  runner_version TEXT
);
INSERT INTO control (id, stopped, reason, updated_at) VALUES (1, 0, '', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor_uid INTEGER,
  actor_name TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  reason TEXT NOT NULL DEFAULT '',
  detail TEXT
);

CREATE TABLE sessions (
  id_hash TEXT PRIMARY KEY,
  uid INTEGER NOT NULL,
  name TEXT NOT NULL,
  korea_changesets INTEGER NOT NULL,
  eligible INTEGER NOT NULL,
  is_admin INTEGER NOT NULL,
  csrf TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE oauth_states (
  state_hash TEXT PRIMARY KEY,
  verifier TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  code_version TEXT,
  summary TEXT,
  files TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX runs_started ON runs (started_at DESC);

-- Ledger of changesets the bot account made. Only these can be rolled back.
CREATE TABLE bot_changesets (
  id INTEGER PRIMARY KEY,
  run_id TEXT,
  kind TEXT NOT NULL DEFAULT 'sync',
  created_at TEXT NOT NULL,
  comment TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'applied'
);

CREATE TABLE rollback_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  changeset_id INTEGER NOT NULL REFERENCES bot_changesets (id),
  status TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  requested_by_uid INTEGER NOT NULL,
  requested_by_name TEXT NOT NULL,
  reason TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  result TEXT
);
CREATE UNIQUE INDEX one_active_rollback ON rollback_jobs (changeset_id) WHERE status IN ('queued', 'running');
