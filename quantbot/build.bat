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
REM   build.bat              - onedir build (default, recommended)
REM   build.bat --onefile    - single exe (extracts to Temp on every run;
REM                            antivirus can lock .pyd files and break imports)
REM   build.bat --console    - debug build with a console window
REM   build.bat --clean      - remove build/ dist/ *.spec first
REM ---------------------------------------------------------------------

python -m tools.build_exe %*
exit /b %ERRORLEVEL%
