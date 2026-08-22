@echo off
REM ---------------------------------------------------------------------
REM QuantBot .exe build launcher
REM
REM NOTE: This file is intentionally ASCII-only and does NOT call "chcp".
REM       cmd.exe re-reads a running batch file by byte offset, so changing
REM       the code page mid-file corrupts the remaining lines. All Korean
REM       messages and the real build logic live in tools/build_exe.py.
REM
REM Usage:
REM   build.bat              - console build (default)
REM   build.bat --windowed   - no console window (for future tray GUI)
REM   build.bat --clean      - remove build/ dist/ *.spec first
REM ---------------------------------------------------------------------

python -m tools.build_exe %*
exit /b %ERRORLEVEL%
