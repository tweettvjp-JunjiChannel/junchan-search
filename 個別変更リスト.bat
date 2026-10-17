@echo off
REM 特定のnote記事だけをWordPressへピンポイントで強制同期するバッチ。
REM 同じフォルダの update_notelist.txt に、対象記事のURLを1行に1つ書いてから
REM ダブルクリックで実行する（空行・"#"で始まる行は無視される）。
REM   - WordPressに既にある記事 : 差分の有無に関係なく最新内容で上書き更新
REM   - WordPressに無い記事     : 新規記事として投稿
REM 試しに内容だけ確認したい場合は、末尾の --execute を外して実行するとドライラン
REM （何も変更しない）になる。

cd /d "%~dp0"

REM Python仮想環境があれば有効化する（無ければシステムのPythonをそのまま使う）
if exist "venv\Scripts\activate.bat" call "venv\Scripts\activate.bat"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

if not exist "update_notelist.txt" (
    echo update_notelist.txt が見つかりません。対象記事のURLを1行に1つ記入して作成してください。
    powershell -NoProfile -Command "[Console]::Beep(400,800)"
    pause
    exit /b 1
)

python sync_updated_notes.py --execute --list-file update_notelist.txt

echo.
if errorlevel 1 (
    powershell -NoProfile -Command "[Console]::Beep(400,800)"
) else (
    powershell -NoProfile -Command "1..3 | ForEach-Object { [Console]::Beep(1000,300); Start-Sleep -Milliseconds 200 }"
)
pause
