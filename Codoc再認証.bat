@echo off
REM Codocの二段階認証（30日ごと）を手動で再設定するバッチ。
REM 画面付きのChromeが開くので、ログイン→メールの6桁コード入力
REM （「このデバイスを30日間信頼する」にチェック）を行い、
REM 終わったらこの黒い画面に戻ってEnterを押す。
REM 週次実行.bat等の同期処理が動いている間は実行しないこと。

cd /d "%~dp0"

if exist "venv\Scripts\activate.bat" call "venv\Scripts\activate.bat"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

REM Pythonの出力はUTF-8のため、文字化け（「□グイン」等）を防ぐようコンソールをUTF-8に切り替える
chcp 65001 > nul
python codoc_reauth.py

echo.
if errorlevel 1 (
    powershell -NoProfile -Command "[Console]::Beep(400,800)"
) else (
    powershell -NoProfile -Command "1..3 | ForEach-Object { [Console]::Beep(1000,300); Start-Sleep -Milliseconds 200 }"
)
pause
