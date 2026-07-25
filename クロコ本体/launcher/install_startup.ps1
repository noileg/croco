# スタートアップフォルダにクロコのショートカットを作る（仕様書2.5章-2）。
#
# 実体はここに置いたままにして、スタートアップには .lnk だけを置く。
# 起動対象を差し替えたくなったときに、スタートアップ側を触らずに済むため。
#
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Remove

param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$launcher = Join-Path $PSScriptRoot 'croco.bat'
$startup  = [Environment]::GetFolderPath('Startup')
$shortcut = Join-Path $startup 'クロコ.lnk'

if ($Remove) {
    if (Test-Path $shortcut) {
        Remove-Item $shortcut -Confirm:$false
        Write-Host "削除しました: $shortcut"
    } else {
        Write-Host "ショートカットは存在しません: $shortcut"
    }
    return
}

if (-not (Test-Path $launcher)) {
    throw "ランチャが見つかりません: $launcher"
}

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath       = $launcher
$link.WorkingDirectory = Split-Path $launcher -Parent
$link.Description      = 'クロコ（Notion + Gemini + Claude Code パイプライン）'
$link.Save()

Write-Host "作成しました: $shortcut"
Write-Host "  -> $launcher"
Write-Host ""
Write-Host "次回のPC起動時からクロコが自動で立ち上がります。"
Write-Host "止めたいときは -Remove を付けて実行してください。"
