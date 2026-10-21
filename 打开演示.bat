@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "VRChat跳舞房助手.exe" (
  echo 请将完整的 Windows 测试包解压后，再运行本入口。
  pause
  exit /b 1
)
start "跳舞房助手 UI 演示" "VRChat跳舞房助手.exe" --demo --state-dir ".demo-state"
