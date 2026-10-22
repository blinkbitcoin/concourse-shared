FROM node:24.22.1-bookworm@sha256:6449e19d8184d84062525b6a7524def03146c2cb04a7f08b076680d0e8e4240a AS trust-base

# Build from the repository root so all images use the same admission gate.
COPY images/trust /usr/local/lib/ci-trust
RUN apt-get -o APT::Update::Error-Mode=any update \
  && apt-get install -y --no-remove --no-install-recommends python3 ca-certificates curl \
  && /usr/bin/python3 /usr/local/lib/ci-trust/verify.py refresh --node

FROM trust-base AS final

RUN npm i --global @graphql-inspector/cli graphql
RUN npm install -g pnpm

RUN apt-get update && apt-get install -y \
  make git curl python3 jq rsync wget ca-certificates gnupg lsb-release gettext-base protobuf-compiler build-essential postgresql

RUN wget https://github.com/mikefarah/yq/releases/download/v4.17.2/yq_linux_386.tar.gz -O - |\
  tar xz && mv yq_linux_386 /usr/bin/yq

RUN apt-get remove -y docker docker.io runc
RUN curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
RUN echo  "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian \
  $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
   && apt-get update \
   && apt-get install -y docker-ce docker-ce-cli containerd.io

RUN mkdir -p /usr/local/lib/docker/cli-plugins \
  && curl -SL https://github.com/docker/compose/releases/download/v2.0.1/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose \
  && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Install gcloud
ENV CLOUDSDK_CORE_DISABLE_PROMPTS 1
ENV CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE /etc/ssl/certs/ca-certificates.crt
RUN curl -sSL https://sdk.cloud.google.com | bash
ENV PATH $PATH:/root/google-cloud-sdk/bin

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

RUN wget https://github.com/mike-engel/jwt-cli/releases/download/4.0.0/jwt-linux.tar.gz \
  && echo "6b0740c3f4c7134a0cbcf802b95b033bd2246d592ad16aa2ee2d80e5b289b4d6  jwt-linux.tar.gz" > jwt-linux.sha256 \
  && shasum --check --status ./jwt-linux.sha256 \
  && tar -xzf jwt-linux.tar.gz \
  && mv jwt /usr/local/bin/jwt

RUN /usr/bin/python3 /usr/local/lib/ci-trust/verify.py verify --node
