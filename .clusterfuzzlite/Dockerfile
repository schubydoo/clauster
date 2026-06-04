# ClusterFuzzLite build image. base-builder-python ships Atheris and the
# compile_python_fuzzer helper preinstalled, so the project just needs to be
# copied in and installed. https://google.github.io/clusterfuzzlite/
# Intentionally an unpinned tag, not a digest. This is the OSS-Fuzz/ClusterFuzzLite
# build-time base image (the documented form: https://google.github.io/clusterfuzzlite/
# build-integration/python-lang/). It's a moving, frequently-GC'd image that the
# CFLite tooling assumes is current; pinning to a digest empirically breaks the
# buildx pull ("failed to resolve ... unknown type text/html") and rots quickly.
# It builds the fuzzers only — it is never shipped or run as a release artifact.
# hadolint ignore=DL3006
FROM gcr.io/oss-fuzz-base/base-builder-python

# Fuzz the checked-out source (CFLite builds the PR/branch under test), not a
# pinned clone — so harnesses exercise the code actually being changed.
COPY . $SRC/clauster
WORKDIR $SRC/clauster
COPY .clusterfuzzlite/build.sh $SRC/
