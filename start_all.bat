@echo off
cd /d "%~dp0"

rem 重置 PATH，避免从 Git Bash 等环境启动时系统工具被 GNU 同名工具遮蔽
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0"

title CapsWriter 语音输入

echo ============================================================
echo   CapsWriter 语音输入 - 启动
echo ============================================================
echo   本窗口会同时显示客户端与服务端的日志。
echo   直接关闭本窗口 = 客户端和服务端一起关闭。
echo ============================================================
echo.

start "CapsWriter" "%~dp0start_client.exe"
exit
