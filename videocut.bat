@echo off
rem Launch the video cut tool (run "uv sync" once beforehand).
rem Drop a video file onto this file to open it directly.
if not exist "%~dp0.venv\Scripts\videocut.exe" (
  echo Run "uv sync" in %~dp0 first.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\videocut.exe" %*
