@echo off
REM Spacescraper launcher for Windows.
REM
REM Usage:
REM   start_all.bat                 Start the API and all worker nodes
REM   start_all.bat --api-only      Start only the API gateway
REM   start_all.bat --workers-only  Start only the worker nodes
REM
REM For a single headless scrape with no long-running process, use the CLI:
REM   python cli.py scrape https://example.com --pretty

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python was not found on PATH.
    exit /b 2
)

echo [Spacescraper] Checking dependencies...
python cli.py health
if errorlevel 3 (
    echo [ERROR] Health check failed. Resolve the issues above before starting.
    exit /b 3
)

echo [Spacescraper] Starting cluster...
python boot.py %*
exit /b %errorlevel%
