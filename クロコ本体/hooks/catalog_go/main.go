// PostToolUseフック：書き込まれた/読まれたファイルを資料管理DB(items)へ機械的に登録する。
// hooks/catalog.py（Python版）のGo移植。設計の経緯はcatalog.pyのdocstringと
// `記録/検討中_資料管理DB化.md`を見ること。Python版は起動が約50msかかり、Read/Edit/Write
// のたびに走る高頻度フックなので、file_watch系（ノート監視フック書き直し）と同じ理由で移した。
//
// 埋めるのは path・project・updated_at だけ。stage/status/lore は判断が要るため自動では埋めず、
// 「載っていること」だけを機械的に保証する（新規Write・Read時は埋めるよう促す通知を出す）。
//
// 失敗しても作業は止めない（croco全体の「失敗したら何もしない」方針）。ただし記録だけは
// logs/catalog_hook_errors.log に追記する。
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type hookInput struct {
	SessionID string `json:"session_id"`
	ToolName  string `json:"tool_name"`
	ToolInput struct {
		FilePath string `json:"file_path"`
	} `json:"tool_input"`
}

var trackedTools = map[string]bool{"Edit": true, "Write": true, "NotebookEdit": true, "Read": true}

var cfg = resolvePaths()

func logError(context string, err error) {
	defer func() { recover() }()
	if os.MkdirAll(filepath.Dir(cfg.logPath), 0755) != nil {
		return
	}
	f, e := os.OpenFile(cfg.logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if e != nil {
		return
	}
	defer f.Close()
	line := fmt.Sprintf("%s %s: %v\n", time.Now().Format("2006-01-02T15:04:05"), context, err)
	f.WriteString(strings.ToValidUTF8(line, "�"))
}

func emit(text string) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.Encode(map[string]interface{}{
		"hookSpecificOutput": map[string]string{
			"hookEventName":     "PostToolUse",
			"additionalContext": text,
		},
	})
	os.Stdout.Write(buf.Bytes())
}

func main() {
	raw, err := io.ReadAll(os.Stdin)
	if err != nil {
		return
	}
	var in hookInput
	if json.Unmarshal(raw, &in) != nil {
		return
	}
	if !trackedTools[in.ToolName] || in.ToolInput.FilePath == "" {
		return
	}
	filePath := in.ToolInput.FilePath

	db, err := openDB(cfg.dbPath)
	if err != nil {
		logError("main", err)
		return
	}
	defer db.Close()

	existing, err := db.getItem(filePath)
	if err != nil {
		logError("main", err)
		return
	}
	isNew := existing == nil

	// Read時、既に台帳にあるファイルはupsertしない（2026-08-21、本人指摘:
	// 「AIがreadした履歴はいらない」）。書き込み頻度を減らしDBロック競合の機会も減らす。
	if in.ToolName != "Read" || isNew {
		if err := db.upsertItem(filePath, cfg.projectForPath(filePath),
			time.Now().Format("2006-01-02T15:04:05")); err != nil {
			logError(fmt.Sprintf("upsert_item(%s)", filePath), err)
		}
	}

	if err := shadowTrackRelated(db, filePath, in.SessionID); err != nil {
		logError(fmt.Sprintf("shadow_track_related(%s)", filePath), err)
	}

	// 新規Writeのときだけ、気づきを注入する（Edit/NotebookEditや既存ファイルの上書きは対象外）。
	if in.ToolName == "Write" && isNew {
		var notices []string
		for _, n := range []string{
			basenameWarning(db, filePath),
			newFolderLoreReminder(cfg, filePath),
			newFileRegisterReminder(cfg, filePath),
		} {
			if n != "" {
				notices = append(notices, n)
			}
		}
		if len(notices) > 0 {
			emit(strings.Join(notices, "\n\n"))
		}
	}

	// Read時：週次クォータが高いときは出さない（Writeの新規登録促しは対象外）。
	if in.ToolName == "Read" && !weeklyQuotaExceeded() {
		msg, err := readMissingRegisterReminder(cfg, db, filePath, in.SessionID)
		if err != nil {
			logError(fmt.Sprintf("read_missing_register_reminder(%s)", filePath), err)
		} else if msg != "" {
			emit(msg)
		}
	}
}

// ---- file_watchとの連携（shadow-tracking、2026-08-20） ----
//
// あるファイルを触ったとき、そのファイルが依存している先（outgoing relations、1ホップのみ）の
// 中身も file_watch の監視対象に加える。file_watch側は改造せず、向こうが見ているstateファイル
// (~/.claude/tmp/file_watch/{session_id}.json)へスナップショットを追記するだけ。
// 形式は claude-file-watch-hooks の Snapshot{hash, content} と同じ。

// file_watch_track と同じ集合（別モジュールをimportすると壊れやすいので複製している）。
var textExtensions = map[string]bool{
	".txt": true, ".md": true, ".markdown": true,
	".py": true, ".pyw": true, ".js": true, ".mjs": true, ".cjs": true, ".ts": true, ".tsx": true, ".jsx": true,
	".json": true, ".yaml": true, ".yml": true, ".toml": true, ".ini": true, ".cfg": true,
	".html": true, ".htm": true, ".css": true, ".scss": true,
	".csv": true, ".tsv": true,
	".ps1": true, ".sh": true, ".bash": true, ".bat": true,
	".java": true, ".c": true, ".h": true, ".hpp": true, ".cpp": true, ".cs": true, ".go": true, ".rs": true, ".rb": true, ".php": true,
	".sql": true, ".xml": true, ".log": true,
}

type snapshot struct {
	Hash    string `json:"hash"`
	Content string `json:"content"`
}

func fileWatchStateDir() string {
	if v := os.Getenv("FILE_WATCH_STATE_DIR"); v != "" {
		return v
	}
	return homeClaude("tmp", "file_watch")
}

func writeJSONAtomic(path string, v interface{}) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return err
	}
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	tmp := fmt.Sprintf("%s.%d.tmp", path, os.Getpid())
	if err := os.WriteFile(tmp, b, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// 既に追跡中(直接Read済み、または既にshadow済み)のパスは上書きしない
// ——上書きすると、まだcheck側に見せていない変更を黙って握りつぶしてしまう。
func shadowTrackRelated(db *catalogDB, filePath, sessionID string) error {
	if sessionID == "" {
		return nil
	}
	targets, err := db.outgoingTargets(filePath)
	if err != nil || len(targets) == 0 {
		return err
	}

	statePath := filepath.Join(fileWatchStateDir(), sessionID+".json")
	state := map[string]json.RawMessage{}
	if b, err := os.ReadFile(statePath); err == nil {
		if json.Unmarshal(b, &state) != nil {
			state = map[string]json.RawMessage{}
		}
	}

	changed := false
	for _, t := range targets {
		if _, ok := state[t]; ok {
			continue
		}
		if !textExtensions[strings.ToLower(filepath.Ext(t))] {
			continue
		}
		if info, err := os.Stat(t); err != nil || info.IsDir() {
			continue
		}
		b, err := os.ReadFile(t)
		if err != nil {
			continue
		}
		content := strings.ToValidUTF8(string(b), "�")
		sum := sha256.Sum256([]byte(content))
		entry, err := json.Marshal(snapshot{Hash: hex.EncodeToString(sum[:]), Content: content})
		if err != nil {
			continue
		}
		state[t] = entry
		changed = true
	}
	if !changed {
		return nil
	}
	return writeJSONAtomic(statePath, state)
}
