FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ARG DEBIAN_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG DEBIAN_SECURITY_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG DOCKER_APT_MIRROR=mirrors.aliyun.com/docker-ce

WORKDIR /app

RUN . /etc/os-release \
    && codename="${VERSION_CODENAME:-bookworm}" \
    && cat > /etc/apt/sources.list.d/debian.sources <<EOF
Types: deb
URIs: https://${DEBIAN_MIRROR}/debian
Suites: ${codename} ${codename}-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: https://${DEBIAN_SECURITY_MIRROR}/debian-security
Suites: ${codename}-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl --http1.1 --retry 5 --retry-all-errors --retry-delay 2 -fsSL https://${DOCKER_APT_MIRROR}/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && . /etc/os-release \
    && docker_repo_codename="${VERSION_CODENAME}" \
    && case "${docker_repo_codename}" in trixie|forky|sid|unstable) docker_repo_codename="bookworm" ;; esac \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://${DOCKER_APT_MIRROR}/linux/debian ${docker_repo_codename} stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && if [ -x /usr/libexec/docker/cli-plugins/docker-compose ]; then ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose; fi \
    && if [ -x /usr/lib/docker/cli-plugins/docker-compose ]; then ln -sf /usr/lib/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose; fi \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt

# 配置pip使用清华大学镜像源
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app /app/app
COPY babel.cfg /app/babel.cfg

# Compile .mo translation files
RUN pybabel compile -d /app/app/locales

EXPOSE 8100

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
