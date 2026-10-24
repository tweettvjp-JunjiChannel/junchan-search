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

REM Pythonの出力はUTF-8のため、文字化け（「□グイン」等）を防ぐようコンソールをUTF-8に切り替える
chcp 65001 > nul
python sync_updated_notes.py --execute --list-file update_notelist.txt
set SYNCERR=%errorlevel%

rem [2026-10-20] Obsidian: re-fetch the listed note articles and rebuild their notes, then add new articles.
rem Summaries are reused when a topic is unchanged; changed topics and titles are re-summarized by DeepSeek.
echo.
echo [Obsidian] note -^> Obsidian (listed articles are rebuilt, new articles are added)
pushd F:\ArchiveProject
python scripts\note_to_obsidian.py --refresh-list "%~dp0update_notelist.txt"
if not errorlevel 1 python scripts\ai_enrich_note.py --workers 16
if errorlevel 1 echo [Obsidian] ERROR - run F:\ArchiveProject\note*.bat again later to resume.
echo [Obsidian] ExciteBlog -^> Obsidian (new articles only)
python scripts\exblog_to_obsidian.py
if errorlevel 1 echo [Obsidian] ERROR - ExciteBlog import failed. Run it again later to resume.
popd

echo.
if not "%SYNCERR%"=="0" (
    powershell -NoProfile -Command "[Console]::Beep(400,800)"
) else (
    powershell -NoProfile -Command "1..3 | ForEach-Object { [Console]::Beep(1000,300); Start-Sleep -Milliseconds 200 }"
)
pause
