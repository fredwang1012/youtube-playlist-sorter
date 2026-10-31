@echo off
rem Daily run for Task Scheduler. Appends output to data\run.log (the tool never prints tokens).
rem Optional first argument: graph file, default graphs\mine.yaml.
cd /d "%~dp0.."
if not exist data mkdir data
set GRAPH=%~1
if "%GRAPH%"=="" set GRAPH=graphs\mine.yaml
echo ===== %date% %time% ===== >> data\run.log
py -3.12 -m uv run python -m app.cli run --graph %GRAPH% >> data\run.log 2>&1
exit /b %errorlevel%
