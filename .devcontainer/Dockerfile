ARG IMAGE_VARIANT=ubuntu-22.04
FROM mcr.microsoft.com/devcontainers/base:${IMAGE_VARIANT}

ARG DEBIAN_FRONTEND=noninteractive
ARG USER=vscode
ARG PYTHON_VERSION=3.12
ARG HOME=/home/${USER}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        libbz2-dev \
        libffi-dev \
        liblzma-dev \
        libncursesw5-dev \
        libreadline-dev \
        libsqlite3-dev \
        libssl-dev \
        libxml2-dev \
        libxmlsec1-dev \
        sudo \
        tk-dev \
        wget \
        xz-utils \
        zlib1g-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && usermod -s /usr/bin/zsh ${USER}

USER ${USER}
WORKDIR ${HOME}

ENV PYENV_ROOT=${HOME}/.pyenv
ENV PATH=${PYENV_ROOT}/bin:${PYENV_ROOT}/shims:${HOME}/.local/bin:${PATH}

# Nothing downloaded is executed unverified.
# pyenv: pyenv publishes no release checksums, so it is installed by its
# documented "Basic GitHub Checkout" from a pinned tag over https, and the
# checked-out commit must equal the pinned sha (git's content addressing then
# verifies every file) before anything from it runs.
# (Not named PYENV_VERSION: build args are exported to RUN, and pyenv reads
# PYENV_VERSION as the Python version to select.)
ARG PYENV_GIT_TAG=v2.8.15
ARG PYENV_GIT_COMMIT=03ffc48ef3cd33808899773df586f3cc7f74470a
RUN git -c protocol.allow=never -c protocol.https.allow=always clone --depth 1 \
        --branch "${PYENV_GIT_TAG}" https://github.com/pyenv/pyenv.git "${PYENV_ROOT}" \
    && test "$(git -C "${PYENV_ROOT}" rev-parse HEAD)" = "${PYENV_GIT_COMMIT}" \
    && echo 'eval "$(pyenv init -)"' >> "${HOME}/.zshrc" \
    && pyenv install -v "${PYTHON_VERSION}" \
    && pyenv global "${PYTHON_VERSION}"

# PDM: the installer is fetched from the pinned release tag over https only and
# checked against the sha256 PDM publishes (pdm-project.org/install-pdm.py.sha256)
# before it runs; it then installs that same PDM version.
ARG PDM_VERSION=2.29.7
ARG PDM_INSTALLER_SHA256=8c947c5c9ea7a1b84685d5aff9122d974ffa57d1a499f7873d24c61a0781d3a6
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/install-pdm.py \
        https://raw.githubusercontent.com/pdm-project/pdm/"${PDM_VERSION}"/install-pdm.py \
    && echo "${PDM_INSTALLER_SHA256}  /tmp/install-pdm.py" | sha256sum -c - \
    && python /tmp/install-pdm.py --version "${PDM_VERSION}" \
    && rm -f /tmp/install-pdm.py \
    && mkdir -p "${HOME}/.oh-my-zsh/custom/plugins/pdm" \
    && pdm completion zsh > "${HOME}/.oh-my-zsh/custom/plugins/pdm/_pdm" \
    && sed -i 's|^plugins=(|&pdm |' "${HOME}/.zshrc"

WORKDIR /workspaces
