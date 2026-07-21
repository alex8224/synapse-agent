@echo off
setlocal EnableExtensions
REM Thin launcher: prefer installed console script, then local venv, then uv run.
set "HERE=%~dp0"

where synapse >nul 2>nul
if %ERRORLEVEL%==0 (
  synapse %*
  exit /b %ERRORLEVEL%
)

if exist "%HERE%.venv\Scripts\synapse.exe" (
  "%HERE%.venv\Scripts\synapse.exe" %*
  exit /b %ERRORLEVEL%
)

where uv >nul 2>nul
if %ERRORLEVEL%==0 (
  pushd "%HERE%"
  uv run synapse %*
  set "RC=%ERRORLEVEL%"
  popd
  exit /b %RC%
)

echo synapse not found.
echo Install once:
echo   uv tool install --editable "%HERE%."
echo or:
echo   cd /d "%HERE%" ^& uv sync
echo then ensure .venv\Scripts is on PATH, or re-run this launcher.
exit /b 1
