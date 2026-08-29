# croco_manual.ahk を Windows ログイン時に自動で常駐させるためのショートカットを
# スタートアップフォルダに作る（ClaudeCode.ahk と同じ方式：.lnk が .ahk を直接指す。
# .ahk 拡張子は AutoHotkey v2 に関連付け済みの前提）。
#
# 使い方:
#   powershell -ExecutionPolicy Bypass -File install_manual_hotkey.ps1
#
# 解除は、スタートアップフォルダ（shell:startup）から下記 .lnk を消すだけ。

$ErrorActionPreference = "Stop"

$ahkPath = Join-Path $PSScriptRoot "croco_manual.ahk"
if (-not (Test-Path $ahkPath)) {
    Write-Error "croco_manual.ahk が見つかりません: $ahkPath"
    exit 1
}

$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "croco_manual.ahk - ショートカット.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $ahkPath
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Save()

Write-Host "作成しました: $shortcutPath"
Write-Host "次回のログインから自動的に常駐し、Ctrl+Alt+K が有効になる。"
Write-Host "すぐ効かせるなら（このセッションで既に起動済みなら不要）:"
Write-Host "  & 'C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe' `"$ahkPath`""
