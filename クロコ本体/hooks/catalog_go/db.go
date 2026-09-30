// 資料管理DB（items / relations）へのアクセス。catalog_db.py のうちフックが使う部分だけを移した。
//
// スキーマ・WAL・weight列のマイグレーションはPython版と同じ内容を毎回確認する
// （catalog_cli.pyより先にフックがDBを開いても壊れないようにするため）。
package main

import (
	"database/sql"
	"path/filepath"
	"strings"

	_ "modernc.org/sqlite"
)

const schemaItems = `CREATE TABLE IF NOT EXISTS items (
    path TEXT PRIMARY KEY,
    project TEXT,
    stage TEXT,
    status TEXT,
    kind TEXT,
    lore TEXT,
    updated_at TEXT NOT NULL
)`

const schemaRelations = `CREATE TABLE IF NOT EXISTS relations (
    from_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    to_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1
)`

type item struct {
	Path string
	Lore string
}

type catalogDB struct{ db *sql.DB }

func openDB(path string) (*catalogDB, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	// PRAGMAは接続ごとの設定なので、接続を1本に固定して確実に効かせる。
	db.SetMaxOpenConns(1)
	// Python版sqlite3の既定タイムアウト(5秒)と揃える。
	stmts := []string{
		"PRAGMA busy_timeout=5000",
		"PRAGMA journal_mode=WAL",
		schemaItems,
		schemaRelations,
		"CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_id)",
		"CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id)",
	}
	for _, s := range stmts {
		if _, err := db.Exec(s); err != nil {
			db.Close()
			return nil, err
		}
	}
	// 2026-08-20以前のDBにweight列が無ければ足す（既存行はDEFAULT 1で埋まる）。
	rows, err := db.Query("PRAGMA table_info(relations)")
	if err != nil {
		db.Close()
		return nil, err
	}
	hasWeight := false
	for rows.Next() {
		var cid int
		var name, typ string
		var notnull, pk int
		var dflt sql.NullString
		if err := rows.Scan(&cid, &name, &typ, &notnull, &dflt, &pk); err == nil && name == "weight" {
			hasWeight = true
		}
	}
	rows.Close()
	if !hasWeight {
		if _, err := db.Exec("ALTER TABLE relations ADD COLUMN weight INTEGER NOT NULL DEFAULT 1"); err != nil {
			db.Close()
			return nil, err
		}
	}
	return &catalogDB{db: db}, nil
}

func (c *catalogDB) Close() { c.db.Close() }

// 台帳への機械的な登録・更新。stage/status/loreは触らない（判断が要るため）。
func (c *catalogDB) upsertItem(path string, project *string, updatedAt string) error {
	var proj interface{}
	if project != nil {
		proj = *project
	}
	_, err := c.db.Exec(`
INSERT INTO items (path, project, updated_at)
VALUES (?, ?, ?)
ON CONFLICT(path) DO UPDATE SET
    project = excluded.project,
    updated_at = excluded.updated_at`, path, proj, updatedAt)
	return err
}

// 未登録ならnil。
func (c *catalogDB) getItem(path string) (*item, error) {
	var lore sql.NullString
	err := c.db.QueryRow("SELECT lore FROM items WHERE path = ?", path).Scan(&lore)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &item{Path: path, Lore: lore.String}, nil
}

// from/toどちらに立っていても関係が1件でもあるか。
func (c *catalogDB) hasRelations(path string) (bool, error) {
	var n int
	err := c.db.QueryRow(
		"SELECT COUNT(*) FROM relations WHERE from_id = ? OR to_id = ?", path, path).Scan(&n)
	return n > 0, err
}

// pathがfrom側に立っている関係の相手（1ホップのみ）。
func (c *catalogDB) outgoingTargets(path string) ([]string, error) {
	rows, err := c.db.Query("SELECT DISTINCT to_id FROM relations WHERE from_id = ?", path)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var t string
		if err := rows.Scan(&t); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

// 同じファイル名（拡張子込み、パス除く）を持つ他のitems。
func (c *catalogDB) findByBasename(path string) ([]string, error) {
	name := filepath.Base(path)
	esc := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`).Replace(name)
	rows, err := c.db.Query(
		`SELECT path FROM items WHERE path != ? AND path LIKE '%' || ? ESCAPE '\'`, path, esc)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var p string
		if err := rows.Scan(&p); err != nil {
			return nil, err
		}
		// LIKEの部分一致は末尾一致まで保証しないため、basenameが完全一致するものだけに絞る。
		if filepath.Base(p) == name {
			out = append(out, p)
		}
	}
	return out, rows.Err()
}
