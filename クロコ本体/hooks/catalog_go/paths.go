package main

import (
	"os"
	"path/filepath"
	"strings"
)

// 置き場所は実行ファイルの位置から決める（Python版が__file__から決めているのと同じ）。
// exeは `クロコ本体/hooks/` に置く前提で、その2つ上が「クロコ本体」。
// テスト時は CATALOG_BASE_DIR で「クロコ本体」相当のフォルダを差し替えられる。
type paths struct {
	base         string // クロコ本体
	dbPath       string
	projectsRoot string
	logPath      string
}

func resolvePaths() paths {
	base := os.Getenv("CATALOG_BASE_DIR")
	if base == "" {
		exe, err := os.Executable()
		if err != nil {
			exe = "."
		}
		base = filepath.Dir(filepath.Dir(exe))
	}
	return paths{
		base:         base,
		dbPath:       filepath.Join(base, "catalog.db"),
		projectsRoot: filepath.Join(filepath.Dir(base), "クロコ管轄プロジェクト"),
		logPath:      filepath.Join(base, "logs", "catalog_hook_errors.log"),
	}
}

func splitParts(p string) []string {
	p = filepath.Clean(p)
	vol := filepath.VolumeName(p)
	p = strings.TrimPrefix(p, vol)
	var parts []string
	for _, s := range strings.Split(p, string(filepath.Separator)) {
		if s != "" {
			parts = append(parts, s)
		}
	}
	return parts
}

// projectsRoot配下ならその相対パスの各要素を返す。Windowsのパスは大文字小文字を区別しない
// （Python版 Path.relative_to の挙動に合わせる）。
func (p paths) relToProjects(path string) ([]string, bool) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, false
	}
	if !strings.EqualFold(filepath.VolumeName(abs), filepath.VolumeName(p.projectsRoot)) {
		return nil, false
	}
	a, r := splitParts(abs), splitParts(p.projectsRoot)
	if len(a) < len(r) {
		return nil, false
	}
	for i := range r {
		if !strings.EqualFold(a[i], r[i]) {
			return nil, false
		}
	}
	return a[len(r):], true
}

// クロコ管轄プロジェクト直下のトップレベルフォルダ名。対象外ならnil。
func (p paths) projectForPath(path string) *string {
	rel, ok := p.relToProjects(path)
	if !ok || len(rel) == 0 {
		return nil
	}
	return &rel[0]
}

// クロコ管轄プロジェクト直下フォルダ自身のREADME.mdか。
func (p paths) isProjectReadme(path string) bool {
	rel, ok := p.relToProjects(path)
	return ok && len(rel) == 2 && rel[1] == "README.md"
}
