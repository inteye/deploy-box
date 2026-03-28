# DeployBox

<p align="center">
  <strong>面向 Docker / Docker Compose 项目的开源发布控制平面。</strong>
</p>

<p align="center">
  <a href="./README.md">English</a> |
  <a href="./README.zh-CN.md">简体中文</a>
</p>

[![Python](https://img.shields.io/badge/python-3.13+-blue.svg)](#)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.116+-009688.svg)](#)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED.svg)](#)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](#license)

DeployBox 将**项目接入、制品打包、版本发布、远端部署跟踪**整合在一个轻量控制台中。

它面向希望快速落地发布流程、但不想引入重型 CI/CD 平台的团队。

## 核心特性

- 在一个控制台管理多个项目和发布流程
- 组件级发布（只发布本次变更的镜像）
- 支持从 `docker-compose.yml` 导入组件候选
- 内置制品链路（`manifest.json` + 镜像 tar）
- 异步部署跟踪、日志查看、回滚入口
- 支持本地制品站和 OSS

## 系统架构

```mermaid
flowchart LR
    U[用户] --> C[DeployBox Console]
    C --> DB[(SQLite / Data)]
    C --> A[Artifacts Server]
    C --> W[Webhook Adapter]
    W --> R[Deploy Agent]
    R --> H[目标 Docker 主机]
    A --> R
```

## 发布时序

```mermaid
sequenceDiagram
    participant User as 用户
    participant Console as DeployBox Console
    participant Artifacts as Artifacts/OSS
    participant Agent as deploy-agent
    participant Host as 目标主机

    User->>Console: 创建构建任务（选择组件）
    Console->>Console: 执行打包脚本
    Console->>Artifacts: 发布 manifest 与 tar
    Console->>Agent: POST /deploy/hook (manifest_url)
    Agent->>Artifacts: 下载 manifest/tar
    Agent->>Host: docker load / docker pull
    Agent->>Host: docker compose up -d ...
    Agent-->>Console: GET /deploy/status（轮询）
    Console-->>User: 返回部署状态/日志/回滚入口
```

## 仓库目录

- `app/`：FastAPI 应用与页面模板
- `docker-compose.yml`：本地启动编排（`deploy-console` + `artifacts`）
- `Dockerfile`：deploy-console 镜像构建文件
- `requirements.txt`：Python 依赖
- `data/`：本地 SQLite 数据
- `dist/releases/`：本地制品目录
- `docs/`：补充文档

## 快速开始

1. 创建环境变量文件：

```bash
cp .env.example .env
```

2. 修改 `.env` 关键项：

- `DEPLOY_CONSOLE_SECRET_KEY`
- `DEPLOY_CONSOLE_ADMIN_USERNAME`
- `DEPLOY_CONSOLE_ADMIN_PASSWORD`
- `DEPLOY_CONSOLE_WORKSPACE_HOST_PATH`
- `DEPLOY_CONSOLE_PACKAGE_ARTIFACT_PUBLIC_BASE_URL`

3. 启动 DeployBox：

```bash
docker compose up -d --build
```

4. 访问控制台：

```text
http://127.0.0.1:18101
```

## 关键配置说明

- `DEPLOY_CONSOLE_WORKSPACE_HOST_PATH`
  宿主机工作区根目录（可是真目录或软链接目录）。
- `DEPLOY_CONSOLE_WORKSPACE_PATH`
  容器内工作区路径，默认 `/workspace`。
- `DEPLOY_CONSOLE_PACKAGE_SCRIPT`
  默认打包脚本（相对于项目工作区），默认 `deploy/scripts/package_release.sh`。
- `DEPLOY_CONSOLE_PACKAGE_ARTIFACT_BASE_URL`
  控制台容器访问本地制品站的内部地址。
- `DEPLOY_CONSOLE_PACKAGE_ARTIFACT_PUBLIC_BASE_URL`
  远端 `deploy-agent` 可访问的公开地址。
- `DEPLOY_CONSOLE_LOCAL_ARTIFACTS_PATH`
  控制台容器内本地制品目录，默认 `/artifacts/releases`。
- `USE_OSS`
  全局制品存储开关。

## 制品模式

- `auto`：跟随全局 `USE_OSS`
- `local`：强制本地制品站
- `oss`：强制 OSS

建议：

- 开发/测试：`local`（或 `USE_OSS=false` 下使用 `auto`）
- 生产：显式使用 `oss`

## 项目接入流程

1. 创建项目（`name`、`slug`、工作区路径、镜像仓库前缀）
2. 从 `docker-compose.yml` 导入组件候选
3. 查看 Compose 规范检查，按需下载推荐 compose
4. 维护组件元数据（`service_name`、image、dockerfile、context、构建模式）
5. 配置环境（`webhook_url`、`status_url`、`shared_secret`）
6. 生成 release 并异步部署

## Compose 发布规范建议

为确保容器实际切换到新镜像：

- 可发布服务优先使用 `image:`
- 生产发布链路尽量不依赖 `build:`
- 用 `DEPLOY_IMAGE_<SERVICE>` 引用发布镜像
- 避免源码挂载覆盖镜像文件系统（如 `./backend:/app`）
- 业务镜像避免长期使用 `latest`

示例：

```yaml
services:
  backend:
    image: ${DEPLOY_IMAGE_BACKEND:-registry.example.com/myapp-backend:latest}
```

`deploy-agent` 会根据 manifest 自动注入 `DEPLOY_IMAGE_<SERVICE>`。

## deploy-agent 接入

starter 包会生成：

- `deploy/deploy-agent/Dockerfile`
- `deploy/deploy-agent/deploy-agent.compose.yml`
- `deploy/deploy-agent/deploy-agent.env.example`
- `deploy/deploy-agent/bin/deploy-release.sh`

最小启动方式：

```bash
cd deploy/deploy-agent
cp deploy-agent.env.example deploy-agent.env
docker compose -f deploy-agent.compose.yml up -d --build
```

关键变量：

- `DEPLOY_PROJECT_ROOT=/workspace`
- `DEPLOY_PROJECT_WORKSPACE_HOST_PATH=/宿主机项目绝对路径`

注意：

- 必须填写宿主机真实路径，不是容器内路径
- Docker Desktop 需要把该目录加入 File Sharing
- DeployBox 会用该路径做挂载和 `docker compose --project-directory`

## 本地开发

常规启动：

```bash
docker compose up -d --build
```

依赖或 Dockerfile 有变化时：

```bash
docker compose build --no-cache deploy-console
docker compose up -d deploy-console
```

## Roadmap

- 更强的 Compose 规范检查与兼容性分析
- Kubernetes / Helm 适配器
- 更细粒度的审计与审批能力
- 更多存储后端与 CDN 集成

## 参与贡献

欢迎提交 Issue 与 PR，重点方向：

- 部署可靠性
- 接入体验
- 可观测性
- 适配器扩展能力

## License

MIT
