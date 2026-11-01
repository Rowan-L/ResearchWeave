@echo off
rem E3 一键防回归检查:ruff + 两组测试(按 B20 教训分组跑 UI 验收)
set PY=C:\Users\1\.conda\envs\SD\python.exe
cd /d %~dp0..

%PY% -m ruff check src tests
if errorlevel 1 goto :fail

%PY% -m pytest tests --ignore=tests/test_ui_acceptance.py -q
if errorlevel 1 goto :fail

%PY% -m pytest tests/test_ui_acceptance.py -q
if errorlevel 1 goto :fail

echo ============================================
echo  ALL CHECKS PASSED
echo ============================================
exit /b 0

:fail
echo ============================================
echo  CHECKS FAILED
echo ============================================
exit /b 1
