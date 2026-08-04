# Docker 中运行 Web TUI

把 Synapse Web TUI 以 Docker 容器作为沙箱运行：浏览器通过 WebSocket 连接
`textual-serve`，服务端为每个浏览器会话启动一个 `synapse tui` 子进程。

镜像不包含 Rust 工具链，运行时依赖全部通过预编译 wheel 安装，只依赖
`python:3.12-slim` 基础镜像，镜像体积最小。

## 入口

Web 启动器已作为包内模块和独立控制台命令提供：

- `synapse.web`（`python -m synapse.web`）
- `synapse-web`（wheel 安装后直接可用）

```powershell
synapse-web --host 0.0.0.0 --port 8000
```

源码仓库中的 `scripts/serve_web.py` 保留为兼容薄包装，行为一致。

## 镜像构建

### 方式一：从 PyPI 安装发布版本

```bash
docker build -f Dockerfile.web -t synapse-web .
```

默认使用 `pypi` 模式，从 PyPI 安装 `synapse-cli-agent==0.1.23` 及其依赖
（包括已有预编译 wheel 的 `synapse-core-tool`）。

如需指定版本：

```bash
docker build -f Dockerfile.web \
  --build-arg SYNAPSE_VERSION=0.1.23 \
  -t synapse-web .
```

### 方式二：安装本地构建的 wheel

先把本地 wheel 放入构建上下文 `dist/`，然后使用 `local` 模式：

```bash
docker build -f Dockerfile.web --build-arg SYNAPSE_MODE=local -t synapse-web .
```

## 在远程机器上构建

```bash
# 在远程机器 root@gateway1 上执行
scp Dockerfile.web root@gateway1:/tmp/synapse/
ssh root@gateway1
cd /tmp/synapse
docker build -f Dockerfile.web -t synapse-web .
```

## 运行

```bash
docker run --rm -d --name synapse-web \
  -p 8000:8000 \
  -v "$PWD:/workspace" \
  synapse-web
```

然后浏览器访问：

```text
http://<gateway1-ip>:8000
```

`/workspace` 是 agent 的工作目录，也是配置（`.synapse/`）和会话数据的落盘位置，
通过 volume 挂载到宿主机即可持久化。

## 行为说明

- `synapse-web` 进程常驻，负责 HTTP 页面和 WebSocket。
- 浏览器每次刷新会断开旧 WebSocket 并建立新连接，服务端会重新启动一个
  `synapse tui` 子进程；界面与内存状态会重置，但会话、配置和主题等持久化
  数据仍从 `/workspace` 恢复。
- 每个浏览器标签页对应一个独立的 TUI 子进程。
- agent 的工具执行都发生在容器内，适合作为沙箱隔离。
- 容器以非 root 用户 `synapse` 运行，镜像只暴露 `8000` 端口。
