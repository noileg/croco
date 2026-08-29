#Requires AutoHotkey v2.0
#SingleInstance Force

; Ctrl+Alt+K で Windows Terminal を開き、クロコの手動モードを起動する。
;   python run_croco.py --manual
;   （Inbox を一覧表示して、選んだ1件を対話モードのクロコに渡す。
;    0 + Enter で「要確認」の一覧に移る。終了は Ctrl+C）
;
; launcher\croco.bat 経由で呼ぶ（chcp / PYTHONUTF8 の設定と、終了時に
; 画面を残す pause をそのまま流用するため）。通常のクロコ（スタートアップ
; 起動 or croco.bat 引数なし）が動いていても、これは並行で走らせてよい。
;
; ClaudeCode.ahk (Ctrl+Alt+C) / MoveTerminal.ahk (Ctrl+Alt+M) /
; VolumeBoost.ahk (Alt+V) とはホットキーが重複しない。
;
; 常駐させるには：install_manual_hotkey.ps1 を1回実行する
; （スタートアップに .lnk を作るので、次回ログインから自動で常駐する）。
; その場ですぐ効かせたいだけなら、このファイルをダブルクリックしてもよい。

WtExe := EnvGet("LOCALAPPDATA") . "\Microsoft\WindowsApps\wt.exe"
BatFile := EnvGet("USERPROFILE") . "\Desktop\クロコ関係\クロコ本体\launcher\croco.bat"
WorkDir := EnvGet("USERPROFILE") . "\Desktop\クロコ関係\クロコ本体"

^!k:: {
    Run('"' WtExe '" -d "' WorkDir '" "' BatFile '" --manual')
}
