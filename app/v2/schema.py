"""2.0 数据表（schema_version 2）。"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS systems (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL,
    name       TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS apps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    system_id  INTEGER NOT NULL,
    name       TEXT NOT NULL,
    key        TEXT NOT NULL,                 -- 快照包 manifest.app 对应的标识，如 eb-ops-admin
    kind       TEXT NOT NULL DEFAULT 'web',   -- web | flutter_web | h5 | other
    renderer   TEXT NOT NULL DEFAULT 'html',  -- html | image
    base_url   TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE (key)
);

CREATE TABLE IF NOT EXISTS releases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL,
    version     TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    imported_by INTEGER,
    imported_at TEXT NOT NULL,
    UNIQUE (app_id, version)
);

CREATE TABLE IF NOT EXISTS pages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     INTEGER NOT NULL,
    route_key  TEXT NOT NULL,
    title      TEXT NOT NULL,
    is_new     INTEGER NOT NULL DEFAULT 0,    -- 由需求新增、尚未出现在任何快照中
    created_by INTEGER,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE (app_id, route_key)
);

CREATE TABLE IF NOT EXISTS menu_nodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     INTEGER NOT NULL,
    parent_id  INTEGER,
    title      TEXT NOT NULL,
    route_key  TEXT,
    page_id    INTEGER,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_menu_app ON menu_nodes(app_id, parent_id);

CREATE TABLE IF NOT EXISTS page_versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id           INTEGER NOT NULL,
    kind              TEXT NOT NULL,           -- baseline | proposal
    media             TEXT NOT NULL,           -- html | image
    release_id        INTEGER,                 -- baseline 来源
    requirement_id    INTEGER,                 -- proposal 来源
    base_version_id   INTEGER,                 -- proposal 基于的版本
    content_hash      TEXT NOT NULL,
    original_filename TEXT NOT NULL DEFAULT '',
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    note              TEXT NOT NULL DEFAULT '',
    diff_json         TEXT,                    -- 自动 diff 结果
    uploaded_by       INTEGER,
    uploaded_at       TEXT NOT NULL,
    deleted_at        TEXT,
    deleted_by        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pv_page ON page_versions(page_id, id);
CREATE INDEX IF NOT EXISTS idx_pv_req ON page_versions(requirement_id);

CREATE TABLE IF NOT EXISTS snapshot_imports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id       INTEGER NOT NULL,
    release_id   INTEGER NOT NULL,
    filename     TEXT NOT NULL,
    page_count   INTEGER NOT NULL DEFAULT 0,
    new_versions INTEGER NOT NULL DEFAULT 0,
    imported_by  INTEGER,
    imported_at  TEXT NOT NULL
);
"""

# requirements 表新增列（ALTER 需逐条、忽略已存在）
REQUIREMENT_COLUMNS_V2 = {
    "v2_status": "TEXT",       # draft | pending | live（仅 kind='v2'）
    "v2_release": "TEXT",      # 计划/实际上线版本号
    "v2_app_id": "INTEGER",    # 主要涉及的端（用于自动对齐上线）
}

SCHEMA_V2 += """
CREATE TABLE IF NOT EXISTS requirement_apps (
    requirement_id  INTEGER NOT NULL,
    app_id          INTEGER NOT NULL,
    release_version TEXT NOT NULL DEFAULT '',   -- 该需求在此端的计划/实际上线版本
    live_at         TEXT,                       -- 对齐到 release 快照的时间
    PRIMARY KEY (requirement_id, app_id)
);

CREATE TABLE IF NOT EXISTS annotations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    x          REAL NOT NULL,      -- 相对坐标 0~1（相对内容宽/高）
    y          REAL NOT NULL,
    w          REAL NOT NULL,
    h          REAL NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ann_version ON annotations(version_id);
"""

SCHEMA_V2 += """
CREATE TABLE IF NOT EXISTS requirement_pages (
    requirement_id  INTEGER NOT NULL,
    page_id         INTEGER NOT NULL,
    base_version_id INTEGER,
    added_by        INTEGER,
    added_at        TEXT NOT NULL,
    deleted_at      TEXT,
    PRIMARY KEY (requirement_id, page_id)
);
"""
