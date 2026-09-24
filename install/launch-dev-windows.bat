@echo off
REM ===========================================================================
REM  BlueShield - launch Chrome with the unpacked extension on Windows.
REM
REM  Use this when chrome://extensions no longer offers "Load unpacked", or
REM  when the extension keeps being disabled. It uses a SEPARATE profile, so it
REM  never touches your normal Chrome profile.
REM
REM  Usage:  launch-dev-windows.bat [path-to-unpacked-extension]
REM  Default path is ..\release\BlueShield-1.0.0.0
REM ===========================================================================
setlocal

set "EXT=%~1"
if "%EXT%"=="" set "EXT=%~dp0..\release\BlueShield-1.0.0.0"

REM Prefer Chrome for Testing if it is present (no web-store / policy baggage),
REM otherwise fall back to a normal Chrome install.
set "CHROME="
for %%P in (
  "%LOCALAPPDATA%\Google\Chrome for Testing\chrome.exe"
  "%PROGRAMFILES%\Google\Chrome for Testing\chrome.exe"
  "%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"
  "%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"
  "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
) do (
  if exist %%P if not defined CHROME set "CHROME=%%~P"
)

if not defined CHROME (
  echo [BlueShield] No Chrome or Chrome for Testing build was found.
  echo Install Chrome for Testing from:
  echo   https://googlechromelabs.github.io/chrome-for-testing/
  exit /b 1
)

if not exist "%EXT%\manifest.json" (
  echo [BlueShield] No unpacked extension at: %EXT%
  echo Pass the path as the first argument, for example:
  echo   launch-dev-windows.bat C:\blueshield\BlueShield-1.0.0.0
  exit /b 1
)

set "PROFILE=%LOCALAPPDATA%\BlueShield\dev-profile"
if not exist "%PROFILE%" mkdir "%PROFILE%" >nul 2>&1

echo [BlueShield] Chrome : %CHROME%
echo [BlueShield] Profile: %PROFILE%
echo [BlueShield] Loading: %EXT%
echo.

REM --disable-features=DisableLoadExtensionCommandLineSwitch is required from
REM Chrome 137 onwards, where --load-extension is otherwise ignored.
start "" "%CHROME%" ^
  --user-data-dir="%PROFILE%" ^
  --load-extension="%EXT%" ^
  --disable-features=DisableLoadExtensionCommandLineSwitch ^
  --no-first-run ^
  --no-default-browser-check ^
  "chrome://extensions/?errors=1"

endlocal
