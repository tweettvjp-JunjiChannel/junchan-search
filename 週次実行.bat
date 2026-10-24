@echo off
REM note.com新着記事の週次取り込みバッチ。
REM ダブルクリックで本番実行する（①新着記事のダウンロード→WordPress新規投稿→
REM Codoc月額購読プランへの自動紐付け→②サイドバー「キーワードから探す」
REM タグ一覧ページの再生成→③処理結果サマリーの表示、までを自動で行う）。
REM
REM 過去記事の加筆修正・動画差し替え等の差分同期はここには含まれない
REM （全363件のチェックに約3～3.5時間かかるため、日常の週次バッチとは
REM  分離してある）。差分同期が必要な場合は以下を使うこと:
REM   - 全件差分同期.bat        : 全記事対象（夜間放置向け、長時間）
REM   - note差分同期（直近90日）.bat : 直近90日のみの軽量版（数分で完了）

cd /d "%~dp0"
python sync_weekly.py --execute

REM 2026-10-20 追加：WordPress同期の後に、noteの新しい記事をObsidian（Xアーカイブ\00_Inbox\note）へ取り込み、
REM 話題ごとにDeepSeekで要約する。同じログイン済みブラウザを順番に使うので、同期とぶつからない。
echo.
echo [Obsidian] note記事をObsidianへ取り込み＋AI要約（新しい記事だけ）
pushd F:\ArchiveProject
python scripts\note_to_obsidian.py
if not errorlevel 1 python scripts\ai_enrich_note.py --workers 16
if errorlevel 1 echo [Obsidian] note取り込みでエラーが出ました。F:\ArchiveProject\note取り込み.bat を再実行すれば続きから処理します。
echo [Obsidian] エキサイトブログの新着をObsidianへ取り込み＋AI要約（新しい記事が無ければ数秒で終わります）
python scripts\exblog_to_obsidian.py
if errorlevel 1 echo [Obsidian] エキサイトブログ取り込みでエラーが出ました。F:\ArchiveProject\エキサイトブログ取り込み.bat を再実行すれば続きから処理します。
popd

echo.
powershell -NoProfile -Command "1..3 | ForEach-Object { [Console]::Beep(1000,300); Start-Sleep -Milliseconds 200 }"
pause
