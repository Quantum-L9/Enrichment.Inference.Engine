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

RUN curl -fsSL https://pyenv.run | bash \
    && echo 'eval "$(pyenv init -)"' >> ${HOME}/.zshrc \
    && pyenv install -v ${PYTHON_VERSION} \
    && pyenv global ${PYTHON_VERSION}

RUN curl -fsSL https://pdm-project.org/install-pdm.py | python - \
    && mkdir -p ${HOME}/.oh-my-zsh/custom/plugins/pdm \
    && pdm completion zsh > ${HOME}/.oh-my-zsh/custom/plugins/pdm/_pdm \
    && sed -i 's|^plugins=(|&pdm |' ${HOME}/.zshrc

WORKDIR /workspaces
