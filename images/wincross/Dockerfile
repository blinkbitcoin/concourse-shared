FROM rust:bookworm@sha256:48b7f435b8fc6378ce43b44f15e3a47fd8919b2825f835c39bd4bd3da50af5b8 AS trust-base

# Build from the repository root so all images use the same admission gate.
COPY images/trust /usr/local/lib/ci-trust
RUN apt-get -o APT::Update::Error-Mode=any update \
  && apt-get install -y --no-remove --no-install-recommends python3 ca-certificates curl \
  && /usr/bin/python3 /usr/local/lib/ci-trust/verify.py refresh --rustup

FROM trust-base AS final

RUN apt update && apt upgrade -y
RUN apt install -y g++-mingw-w64-x86-64

RUN rustup target add x86_64-pc-windows-gnu
RUN rustup toolchain install stable-x86_64-pc-windows-gnu --force-non-host

RUN /usr/bin/python3 /usr/local/lib/ci-trust/verify.py verify --rustup
