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

# Three steps so every registry download is hash-pinned (Scorecard Pinned-Dependencies,
# alert 149): the BUILD dependencies first (pyproject declares hatchling, and without
# --no-build-isolation pip would fetch it unpinned at build time — the one download that
# executes code during the build), then the runtime dependency set, both with
# --require-hashes; finally clauster itself from the local tree with --no-deps +
# --no-build-isolation (a path install has no registry artifact to hash, and both its
# build and runtime dependencies are already satisfied).
#
# Both requirements files are GENERATED — regenerate on change:
#   runtime (whenever uv.lock moves):
#     uv export --frozen --no-emit-project --extra pty -o .clusterfuzzlite/requirements.txt
#   build (whenever pyproject's [build-system] or the hatchling pin moves):
#     echo "hatchling==<ver>" | uv pip compile - --generate-hashes --no-annotate \
#       -o .clusterfuzzlite/build-requirements.txt
# Drift guards, one per failure mode: an ADDED dependency missing from the export is
# caught by `pip3 check` below (the installed clauster dist's Requires-Dist names it,
# and a non-zero exit aborts under -eu) — loudly, at build time. A version BUMP not
# re-exported is NOT caught here (pyproject's ranges are unbounded minimums the stale
# pins still satisfy); tests/test_ci_change_filter.py's freshness guard compares the
# committed export against uv.lock on every suite run instead.
#
# The `pty` extra is in that export, not a bare dependency set: pty_screen_feed_fuzzer
# drives the real PtyScreen, which lazily imports pyte and raises PyteUnavailableError
# without it — so a bare install would leave that harness failing on every input instead
# of fuzzing. On Linux the extra resolves to pyte alone (pywinpty carries a win32
# marker). pyte is LGPLv3 and is kept out of [project.dependencies] and the Apache-2.0
# frozen binary on purpose; this image is a CI build container for the fuzzers and is
# never shipped or distributed, so installing it here carries no relink obligation. If
# that ever changes, revisit pyproject.toml's note.
pip3 install --require-hashes -r "$SRC/clauster/.clusterfuzzlite/build-requirements.txt"
pip3 install --require-hashes -r "$SRC/clauster/.clusterfuzzlite/requirements.txt"
pip3 install --no-deps --no-build-isolation "$SRC/clauster"
pip3 check

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
