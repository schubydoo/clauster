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

# The `pty` extra, not a bare install: pty_screen_feed_fuzzer drives the real PtyScreen,
# which lazily imports pyte and raises PyteUnavailableError without it — so a bare install
# would leave that harness failing on every input instead of fuzzing. On Linux the extra
# resolves to pyte alone (pywinpty carries a win32 marker). pyte is LGPLv3 and is kept out
# of [project.dependencies] and the Apache-2.0 frozen binary on purpose; this image is a
# CI build container for the fuzzers and is never shipped or distributed, so installing it
# here carries no relink obligation. If that ever changes, revisit pyproject.toml's note.
pip3 install "$SRC/clauster[pty]"

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
