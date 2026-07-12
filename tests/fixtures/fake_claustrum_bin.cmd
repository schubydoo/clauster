@echo off
rem Windows entrypoint for the fake claustrum daemon binary (mirrors
rem fake_claude/claude.cmd): CreateProcess / shutil.which can't resolve or exec the
rem .py script directly on Windows, so tests point the daemon binary at this .cmd.
rem Forwards argv (incl. -token-fd 0 read from stdin) and propagates the exit code.
python "%~dp0fake_claustrum_bin.py" %*
