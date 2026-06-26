#!/bin/bash -eu
# ClusterFuzzLite build script (Python/Atheris).
#
# Install clauster so the harnesses can `import clauster`, then compile every
# fuzz/*_fuzzer.py into a standalone fuzzer in $OUT. compile_python_fuzzer is the
# OSS-Fuzz helper that wraps pyinstaller + the Atheris run wrapper; clauster is
# pure Python (no native extensions), so no extra build flags are needed.
#
# Per harness we ALSO bundle (when present) a seed corpus and a libFuzzer dict:
#   fuzz/seeds/<name>/*   -> $OUT/<name>_seed_corpus.zip  (initial inputs)
#   fuzz/dicts/<name>.dict -> $OUT/<name>.dict            (auto-loaded by the run)
# This bootstraps regex-gated harnesses (e.g. redact / parse_markers) whose
# interesting branches need structured tokens Atheris' random FDP never
# synthesises — without seeds+dict they sit at a 0-element corpus. See fuzz/README.md.

pip3 install "$SRC/clauster"

for fuzzer in "$SRC"/clauster/fuzz/*_fuzzer.py; do
  compile_python_fuzzer "$fuzzer"
  name="$(basename "$fuzzer" .py)"

  # compgen (not `[ -d ]`) so an empty seeds dir can't leave the glob literal and
  # hand `zip` a nonexistent file → nonzero exit aborts the whole build under `-eu`.
  seeds="$SRC/clauster/fuzz/seeds/$name"
  if compgen -G "$seeds/*" >/dev/null; then
    zip -j "$OUT/${name}_seed_corpus.zip" "$seeds"/* >/dev/null
  fi

  dict="$SRC/clauster/fuzz/dicts/${name}.dict"
  if [ -f "$dict" ]; then
    cp "$dict" "$OUT/${name}.dict"
  fi
done
