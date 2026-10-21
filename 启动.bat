@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist "dance_cache_gui.py" goto launch_source
if exist "VRChat跳舞房助手.exe" goto launch_local
if exist "dist\VRChat跳舞房助手\VRChat跳舞房助手.exe" goto launch_bundle

:launch_source
if exist "C:\Users\92153\AppData\Local\Python\bin\python.exe" (
  start "" "C:\Users\92153\AppData\Local\Python\bin\python.exe" "dance_cache_gui.py" %*
  exit /b
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "dance_cache_gui.py" %*
) else (
  python "dance_cache_gui.py" %*
)
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" (
  echo.
  echo 程序退出，错误码 %APP_EXIT_CODE%。请先按 README 安装 Python 3.10+ 与 requirements.txt。
  pause
)
exit /b %APP_EXIT_CODE%

:launch_local
start "" "VRChat跳舞房助手.exe" %*
exit /b

:launch_bundle
start "" "dist\VRChat跳舞房助手\VRChat跳舞房助手.exe" %*
exit /b
