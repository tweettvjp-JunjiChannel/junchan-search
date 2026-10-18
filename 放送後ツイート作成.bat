@echo off
REM 毎週の放送後ツイート作成バッチ。
REM ダブルクリックすると、ツイキャス一般・メンシプの最新録画URL、YouTubeの最新配信URL、
REM WordPressに同期済みの最新note記事のURLと話題（四つ葉・紅葉マークの見出し）を自動で集めて
REM ツイート文を作り、ダウンロードフォルダのPR動画から各話題に合いそうな候補を提案する。
REM 結果は画面に表示され、tweet_drafts フォルダにもテキストで保存される。
REM note記事が未同期と表示された場合は、先に 週次実行.bat か 個別変更リスト.bat を実行すること。

cd /d "%~dp0"

if exist "venv\Scripts\activate.bat" call "venv\Scripts\activate.bat"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

REM Pythonの出力はUTF-8のため、文字化けを防ぐようコンソールをUTF-8に切り替える
chcp 65001 > nul
python post_broadcast_tweet.py

echo.
if errorlevel 1 (
    powershell -NoProfile -Command "[Console]::Beep(400,800)"
) else (
    powershell -NoProfile -Command "1..3 | ForEach-Object { [Console]::Beep(1000,300); Start-Sleep -Milliseconds 200 }"
)
pause
