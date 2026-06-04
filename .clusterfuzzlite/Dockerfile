# ClusterFuzzLite build image. base-builder-python ships Atheris and the
# compile_python_fuzzer helper preinstalled, so the project just needs to be
# copied in and installed. https://google.github.io/clusterfuzzlite/
# Pinned to an immutable digest (Renovate keeps it current). The base image is
# a moving tag upstream; pinning prevents silent build-environment drift.
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:f64740c4f82d8ec1249dcbdd1726d2c0c925735a5424b9fffb2c48479c34c660

# Fuzz the checked-out source (CFLite builds the PR/branch under test), not a
# pinned clone — so harnesses exercise the code actually being changed.
COPY . $SRC/clauster
WORKDIR $SRC/clauster
COPY .clusterfuzzlite/build.sh $SRC/
