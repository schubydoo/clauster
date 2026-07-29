# ClusterFuzzLite build image. base-builder-python ships Atheris and the
# compile_python_fuzzer helper preinstalled, so the project just needs to be
# copied in and installed. https://google.github.io/clusterfuzzlite/
# OSS-Fuzz/ClusterFuzzLite build-time base image — intentionally an unpinned tag,
# not a digest. OSS-Fuzz rebuilds and garbage-collects these base images
# frequently, so a pinned digest rots and breaks the buildx pull once the old
# image is GC'd. This image only builds the fuzzers in CI; it is never shipped or
# run as a release artifact, so the Scorecard pinned-deps finding is dismissed
# (won't fix) rather than fought with a brittle pin.
# hadolint ignore=DL3006
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:485af05cc843949fd60d0e6e78b1a4922e661baf9af5eb595f9b96f563b42748

# Fuzz the checked-out source (CFLite builds the PR/branch under test), not a
# pinned clone — so harnesses exercise the code actually being changed.
COPY . $SRC/clauster
WORKDIR $SRC/clauster
COPY .clusterfuzzlite/build.sh $SRC/
