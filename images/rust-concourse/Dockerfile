FROM rust:bookworm@sha256:48b7f435b8fc6378ce43b44f15e3a47fd8919b2825f835c39bd4bd3da50af5b8 AS trust-base

# Build from the repository root so all images use the same admission gate.
COPY images/trust /usr/local/lib/ci-trust
RUN apt-get -o APT::Update::Error-Mode=any update \
  && apt-get install -y --no-remove --no-install-recommends python3 ca-certificates curl \
  && /usr/bin/python3 /usr/local/lib/ci-trust/verify.py refresh --rustup

FROM trust-base AS final

RUN rustc --version

RUN rustup component add rustfmt
RUN rustup component add clippy

# Nextest requires its published lockfile; this release includes the TLS fixes.
RUN cargo install --locked cargo-nextest --version 0.9.149
RUN cargo install cargo-deny cargo-audit typos-cli sqlx-cli handlebars-cli

# Install gcloud
ENV CLOUDSDK_CORE_DISABLE_PROMPTS 1
ENV CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE /etc/ssl/certs/ca-certificates.crt
RUN curl -sSL https://sdk.cloud.google.com | bash
ENV PATH $PATH:/root/google-cloud-sdk/bin

RUN apt-get update && apt-get install -y \
  make git curl python3 jq rsync wget ca-certificates gnupg lsb-release gettext-base protobuf-compiler bc xxd

ENV YQ_VERSION 4.31.1
RUN wget https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_386.tar.gz -O - |\
  tar xz && mv yq_linux_386 /usr/bin/yq

RUN apt-get remove -y docker docker.io runc
RUN curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
RUN echo  "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian \
  $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
  && apt-get update \
  && apt-get install -y docker-ce docker-ce-cli containerd.io

ENV DOCKER_COMPOSE_VERSION 2.16.0
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
  && curl -SL https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_VERSION}/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose \
  && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

ENV GH_CLI_VERSION 2.31.0
RUN mkdir ghcli && cd ghcli \
  && wget https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_linux_386.tar.gz -O ghcli.tar.gz \
  && tar --strip-components=1 -xf ghcli.tar.gz \
  && mv bin/gh /usr/local/bin && cd ../ && rm -rf ./ghcli

ENV BUF_VERSION 1.4.0
RUN wget https://github.com/bufbuild/buf/releases/download/v${BUF_VERSION}/buf-Linux-x86_64 -O buf \
  && chmod +x buf && mv buf /usr/local/bin

RUN git clone https://github.com/bats-core/bats-core.git \
  && cd bats-core \
  && ./install.sh /usr/local

ENV GHTOKEN_VERSION 2.0.1
RUN wget -O ghtoken \
  https://github.com/Link-/gh-token/releases/download/v2.0.1/linux-amd64 && \
  echo "f76e8cb35f0b04b59073a486cc952e50fa9f1c930a25619ea9abcf44a13165c4  ghtoken" | \
  shasum -c - && \
  chmod u+x ./ghtoken && \
  mv ./ghtoken /usr/local/bin/ghtoken

RUN /usr/bin/python3 /usr/local/lib/ci-trust/verify.py verify --rustup
