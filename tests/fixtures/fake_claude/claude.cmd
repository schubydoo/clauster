@echo off
rem Windows entrypoint for the fake `claude` stub: Windows CreateProcess cannot
rem run the extensionless Python script directly, so tests point the configured
rem binary at this .cmd on Windows. Forwards argv and propagates the exit code.
python "%~dp0claude" %*
