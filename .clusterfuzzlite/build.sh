#!/bin/bash -eu
# ClusterFuzzLite build script (Python/Atheris).
#
# Install clauster so the harnesses can `import clauster`, then compile every
# fuzz/*_fuzzer.py into a standalone fuzzer in $OUT. compile_python_fuzzer is the
# OSS-Fuzz helper that wraps pyinstaller + the Atheris run wrapper; clauster is
# pure Python (no native extensions), so no extra build flags are needed.

pip3 install "$SRC/clauster"

for fuzzer in "$SRC"/clauster/fuzz/*_fuzzer.py; do
  compile_python_fuzzer "$fuzzer"
done
