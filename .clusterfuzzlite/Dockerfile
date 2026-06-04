# ClusterFuzzLite build image. base-builder-python ships Atheris and the
# compile_python_fuzzer helper preinstalled, so the project just needs to be
# copied in and installed. https://google.github.io/clusterfuzzlite/
# OSS-Fuzz/ClusterFuzzLite build-time base image, pinned by digest (Renovate keeps
# it current). NOTE: this must be the image *manifest* digest, not the config-blob
# digest — `docker manifest inspect` lists the config digest first, and pinning to
# that breaks buildx ("unknown type text/html"). The correct one is from
# `docker buildx imagetools inspect <image> --format '{{.Manifest.Digest}}'`.
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:c6a1758f587395684f5d82a8f2c897155f12f208ca7b1c6a1744129084ac4497

# Fuzz the checked-out source (CFLite builds the PR/branch under test), not a
# pinned clone — so harnesses exercise the code actually being changed.
COPY . $SRC/clauster
WORKDIR $SRC/clauster
COPY .clusterfuzzlite/build.sh $SRC/
