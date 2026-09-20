@echo off
REM note.com新着記事の週次取り込みバッチ。
REM ダブルクリックで本番実行する（①新着記事のダウンロード→WordPress新規投稿→
REM Codoc月額購読プランへの自動紐付け→②サイドバー「キーワードから探す」
REM タグ一覧ページの再生成→③処理結果サマリーの表示、までを自動で行う）。
REM
REM 過去記事の加筆修正・動画差し替え等の差分同期はここには含まれない
REM （全363件のチェックに約3〜3.5時間かかるため、日常の週次バッチとは
REM  分離してある）。差分同期が必要な場合は以下を使うこと:
REM   - 全件差分同期.bat        : 全記事対象（夜間放置向け、長時間）
REM   - note差分同期（直近90日）.bat : 直近90日のみの軽量版（数分で完了）

cd /d "%~dp0"
python sync_weekly.py --execute

echo.
powershell -NoProfile -Command "1..3 | ForEach-Object { [Console]::Beep(1000,300); Start-Sleep -Milliseconds 200 }"
pause
