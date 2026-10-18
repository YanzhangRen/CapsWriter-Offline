@echo off
cd /d "%~dp0"

rem 重置 PATH，避免从 Git Bash 等环境启动时系统工具被 GNU 同名工具遮蔽
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0"

title CapsWriter - 全部停止

echo ============================================================
echo   CapsWriter 语音输入 - 全部停止
echo ============================================================
echo.

echo 正在关闭客户端...
taskkill /IM start_client.exe /T /F >nul 2>&1
tasklist /FI "IMAGENAME eq start_client.exe" | findstr /I "start_client.exe" >nul
if errorlevel 1 echo       客户端本来就没有运行。
if not errorlevel 1 echo       客户端已关闭。

echo 正在关闭服务端...
taskkill /IM start_server.exe /T /F >nul 2>&1
tasklist /FI "IMAGENAME eq start_server.exe" | findstr /I "start_server.exe" >nul
if errorlevel 1 echo       服务端本来就没有运行。
if not errorlevel 1 echo       服务端已关闭。

echo.
echo 已全部停止。
timeout /t 3 /nobreak >nul
exit
