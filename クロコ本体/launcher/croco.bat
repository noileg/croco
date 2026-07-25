@echo off
rem クロコの起動用ランチャ。
rem スタートアップにはこのファイルへのショートカットを置く（install_startup.ps1 参照）。
rem %~dp0 で自分の場所から相対的に解決するので、フォルダを移動しても動く。

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

cd /d "%~dp0.."

python run_croco.py %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo クロコが異常終了しました ^(exit=%EXITCODE%^)。
    echo ログ: "%~dp0..\logs"
    echo.
    pause
)

exit /b %EXITCODE%
