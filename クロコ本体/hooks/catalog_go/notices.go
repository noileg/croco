package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// 週次クォータがこの使用率以上ならRead時の登録促しを出さない。
// ~/.claude/hooks/quota_guard_pretool.py のTHRESHOLDと同じ値（本人指定、2026-09-24）。
const weeklyQuotaLimit = 88

func homeClaude(parts ...string) string {
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	return filepath.Join(append([]string{home, ".claude"}, parts...)...)
}

// 週次(7日)クォータが閾値以上ならtrue。
// statusLine経由で書かれた quota_cache.json を読むだけ（外部API呼び出しはしない）。
// キャッシュが読めない・値が不明のときはfalse（＝促しを出す側）に倒す：促しは
// 「やる前提」の既定動作で、止めるのは高使用率が確認できたときだけ。週次はリセットが
// 遅いので5時間枠のような鮮度(fetched_at)判定はせず、リセット時刻を過ぎていれば無効とだけ見る。
func weeklyQuotaExceeded() bool {
	b, err := os.ReadFile(homeClaude("hooks", "quota_cache.json"))
	if err != nil {
		return false
	}
	var cache map[string]interface{}
	if json.Unmarshal(b, &cache) != nil {
		return false
	}
	used, ok := cache["seven_day_used"].(float64)
	if !ok {
		return false
	}
	if resets, ok := cache["seven_day_resets_at"].(float64); ok && resets <= float64(time.Now().Unix()) {
		return false
	}
	return used >= weeklyQuotaLimit
}

// 新規ファイルと同名の既存ファイルが台帳にあれば気づかせる文言。
// 強制（ブロック）はせず、気づきの機会を作るだけ（同名でも無関係なREADME.md等はよくあるため）。
func basenameWarning(db *catalogDB, filePath string) string {
	others, err := db.findByBasename(filePath)
	if err != nil || len(others) == 0 {
		return ""
	}
	var listing []string
	for i, p := range others {
		if i >= 5 {
			break
		}
		listing = append(listing, "  - "+p)
	}
	more := ""
	if len(others) > 5 {
		more = fmt.Sprintf("\n  ...他%d件", len(others)-5)
	}
	return fmt.Sprintf("[台帳] 同名ファイルが既に台帳にあります: %s\n%s%s\n"+
		"複製・専用化・移植の関係があれば `catalog_cli.py relate <新> derived_from <旧>` "+
		"等で繋ぐこと（無関係な同名なら無視してよい）。",
		filepath.Base(filePath), strings.Join(listing, "\n"), more)
}

// 新規フォルダ自身のREADME.mdなら、loreを埋めるよう気づかせる文言。
func newFolderLoreReminder(p paths, filePath string) string {
	if !p.isProjectReadme(filePath) {
		return ""
	}
	return "[台帳] 新規フォルダのREADME.mdです。loreが未設定だと関連判定" +
		"(catalog_related.py)の候補に出てきません。\n" +
		fmt.Sprintf("`python catalog_cli.py set \"%s\" lore \"<1〜3文の要約>\"` で埋めること。", filePath)
}

// 管轄プロジェクト配下の新規ファイルに、lore・relationの登録を「やる前提」で促す。
// 2026-09-24、本人指示：「loreもrelationも台帳登録できるものはやるようにさせといて。
// やるかどうかの判断を向こうにさせるな。やる前提の上で、なお不要なら不要でいい」。
// 例外（一時ファイル・生成物等）に当たるときだけ、飛ばした理由を返答に一言書かせる。
// 新規フォルダのREADME.mdは newFolderLoreReminder が専用文言を出すので対象外。
func newFileRegisterReminder(p paths, filePath string) string {
	if p.projectForPath(filePath) == nil || p.isProjectReadme(filePath) {
		return ""
	}
	return fmt.Sprintf("[台帳] 新規ファイルを登録しました: %s\n"+
		"このファイルについて、以下を行うこと（やるかどうかは検討しない。行う前提）:\n"+
		"  1. lore: `python catalog_cli.py set \"%s\" lore \"<1〜3文で何のファイルか>\"`\n"+
		"  2. relation: 元にしたもの・参照しているもの・置き換えるもの・同じ目的の別ファイルが"+
		"あれば `catalog_cli.py relate <新> <種別> <旧>`"+
		"（種別は `catalog_cli.py values relation_type` の既存値から選ぶ）\n"+
		"一時ファイル・生成物・使い捨てなど、台帳に残す意味が無いと言い切れる場合だけ省いてよい。"+
		"その場合は省いた理由を返答に一言書くこと。",
		filepath.Base(filePath), filePath)
}

func sanitizeID(s string) string {
	if s == "" {
		s = "default"
	}
	var b strings.Builder
	for _, r := range s {
		if (r >= '0' && r <= '9') || (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') {
			b.WriteRune(r)
		} else {
			b.WriteByte('_')
		}
	}
	return b.String()
}

// Readしたファイルの台帳にloreまたはrelationが無ければ、埋めるよう促す。
// 2026-09-24、本人指示：「readでも未登録の要素(relationなど)を追加するように指示をしてくれ」。
// Readは頻度が高いので、同一セッション内では同じファイルに1回しか出さない
// （出しても埋まらなかったファイルで毎回繰り返すと文脈を食うだけのため）。
func readMissingRegisterReminder(p paths, db *catalogDB, filePath, sessionID string) (string, error) {
	if p.projectForPath(filePath) == nil {
		return "", nil
	}
	it, err := db.getItem(filePath)
	if err != nil || it == nil {
		return "", err
	}
	var missing []string
	if strings.TrimSpace(it.Lore) == "" {
		missing = append(missing, "lore")
	}
	has, err := db.hasRelations(filePath)
	if err != nil {
		return "", err
	}
	if !has {
		missing = append(missing, "relation")
	}
	if len(missing) == 0 {
		return "", nil
	}

	flag := homeClaude("tmp", "catalog_read_notice", sanitizeID(sessionID)+".json")
	seen := map[string]bool{}
	if b, err := os.ReadFile(flag); err == nil {
		var list []string
		if json.Unmarshal(b, &list) == nil {
			for _, s := range list {
				seen[s] = true
			}
		}
	}
	if seen[filePath] {
		return "", nil
	}
	seen[filePath] = true
	list := make([]string, 0, len(seen))
	for s := range seen {
		list = append(list, s)
	}
	sort.Strings(list)
	writeJSONAtomic(flag, list) // 失敗しても通知自体は出す

	lines := []string{
		fmt.Sprintf("[台帳] 読んだファイルに未登録の項目があります: %s（未登録: %s）",
			filepath.Base(filePath), strings.Join(missing, " / ")),
		"中身を読んだ今、分かる範囲で埋めること（やるかどうかは検討しない。行う前提）:",
	}
	for _, m := range missing {
		if m == "lore" {
			lines = append(lines, fmt.Sprintf(
				"  - lore: `python catalog_cli.py set \"%s\" lore \"<1〜3文で何のファイルか>\"`", filePath))
		} else {
			lines = append(lines, "  - relation: 元にしたもの・参照しているもの・置き換えるもの・同じ目的の別ファイルが"+
				"分かれば `catalog_cli.py relate <このファイル> <種別> <相手>`"+
				"（種別は `catalog_cli.py values relation_type` の既存値から選ぶ）")
		}
	}
	lines = append(lines, "中身から判断できない、または台帳に残す意味が無いと言い切れる項目だけ省いてよい。"+
		"省いたら理由を返答に一言書くこと。")
	return strings.Join(lines, "\n"), nil
}
