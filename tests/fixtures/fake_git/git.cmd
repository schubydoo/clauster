@echo off
rem Windows entrypoint for the fake `git` stub (mirrors fake_claude/claude.cmd):
rem CreateProcess / shutil.which can't resolve the extensionless Python script on
rem Windows, so tests target this .cmd. Forwards argv and propagates the exit code.
python "%~dp0git" %*
