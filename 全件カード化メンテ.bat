@echo off
REM note記事（約358件）の外部リンク埋め込み一括ブログカード化バックフィルバッチ。
REM ダブルクリックで本番実行する（未変換の記事のみ検出→ブログカード化→
REM Codoc購読プラン紐付けの再確認までを実行）。夜間放置での実行を想定し、
REM 1件ごとにチェックポイント保存・自動リトライ・失敗時も続行する設計。

cd /d "%~dp0"
python backfill_external_blogcards.py --execute
pause
