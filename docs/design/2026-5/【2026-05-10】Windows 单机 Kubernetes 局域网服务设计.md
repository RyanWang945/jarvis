# Windows 单机 Kubernetes 局域网服务设计

日期：2026-05-10

## 1. 背景

目标是在一台 Windows 电脑上搭建一个完整可用的单机 Kubernetes 环境，并把部署在 Kubernetes 内的服务暴露给同一 Wi-Fi 下的其他设备访问。

本设计分两期推进：

1. **一期**：不考虑 GPU，先完成单机 Kubernetes、局域网访问、基础存储、服务发布和可运维能力。
2. **二期**：在一期架构不推倒重来的前提下，接入 NVIDIA GPU，用于模型推理、向量化、语音、图像等 GPU 工作负载。

宿主机约束：

- 操作系统：Windows 11 优先。
- 内存：64GB。
- Kubernetes 可用内存预算：初期最多约 20GB。
- 网络：同一 Wi-Fi 局域网内访问。
- 一期不考虑高可用，不考虑多节点，不考虑公网暴露。

## 2. 设计目标

一期目标：

- Windows 单机可启动完整 Kubernetes 集群。
- 支持部署普通 Web/API/后台任务服务。
- 同 Wi-Fi 设备可以通过 Windows 局域网 IP 或局域网域名访问服务。
- 支持基础 Ingress、Service、ConfigMap、Secret、PVC。
- 支持本地持久化存储。
- 资源使用可控，默认不超过 20GB 内存预算。
- 能以较低维护成本长期运行。

二期目标：

- 支持 Kubernetes 调度 GPU 工作负载。
- Pod 可以通过 `nvidia.com/gpu` 申请 GPU。
- 支持模型推理类服务和 GPU 监控。
- 尽量复用一期的集群、网络和发布方式。

非目标：

- 不做生产级高可用。
- 不在一期接入公网 HTTPS、DDNS、WAF。
- 不在一期做完整日志平台和告警平台。
- 不把 Windows 本身改造成 Kubernetes 节点。
- 不优先追求与云厂商托管 Kubernetes 完全一致。

## 3. 总体结论

推荐方案：

```text
同 Wi-Fi 设备
  -> Windows 局域网 IP / 局域网 DNS 名称
  -> Windows 防火墙放行 80/443/业务端口
  -> WSL2 Ubuntu
  -> K3s 单节点 Kubernetes
  -> Ingress Controller
  -> Service
  -> Pod
```

核心选型：

- **虚拟化层**：WSL2 Ubuntu。
- **Kubernetes 发行版**：K3s。
- **容器运行时**：K3s 内置 containerd。
- **Ingress**：一期使用 K3s 默认 Traefik。
- **本地存储**：一期使用 K3s 默认 local-path-provisioner。
- **局域网暴露**：优先使用 WSL2 mirrored networking；不满足条件时使用 Windows `netsh portproxy`。
- **GPU 二期**：Windows NVIDIA Driver + WSL2 GPU + K3s containerd + NVIDIA device plugin 或 GPU Operator。

## 4. 方案取舍

### 4.1 为什么不优先用 Docker Desktop Kubernetes

Docker Desktop Kubernetes 上手快，但它更适合本地开发验证，不适合作为需要长期局域网提供服务的主环境：

- 网络暴露经常依赖 Docker Desktop 的转发逻辑，长期服务边界不够清晰。
- GPU 接入 Kubernetes 时链路更绕。
- 集群组件可控性不如直接运行 K3s。
- 后续排查 containerd、runtime、device plugin 时不如原生 Linux 环境直接。

Docker Desktop 仍可保留，用于镜像构建、临时容器和开发工具，但不作为本设计的一期主集群。

### 4.2 为什么不优先用 kind / k3d

kind 和 k3d 都适合快速创建测试集群，但它们的 Kubernetes 节点本身运行在 Docker 容器里。

优点：

- 创建和销毁快。
- 适合 CI 或临时验证。
- 对开发者友好。

缺点：

- 局域网暴露服务需要额外端口映射。
- 存储、网络、GPU 都多一层容器嵌套。
- 二期 GPU 调度会更复杂。

因此 kind / k3d 可以作为测试工具，不作为长期服务环境。

### 4.3 为什么选择 WSL2 + K3s

WSL2 + K3s 的优势：

- K3s 是完整 Kubernetes 发行版，单节点运行成本低。
- K3s 内置 containerd、CoreDNS、Traefik、ServiceLB、local-path-provisioner，减少组件拼装。
- 在 WSL2 Ubuntu 中更接近真实 Linux 运维方式。
- 二期接入 NVIDIA GPU 的路径清晰。
- 与 Windows 桌面使用方式兼容，不需要重装 Linux 或双系统。

主要代价：

- WSL2 网络需要专门处理。
- Windows 重启、睡眠、网络切换后，需要验证 WSL2 和 K3s 状态。
- 单机本地盘没有高可用能力，必须做备份。

## 5. 一期架构设计

### 5.1 组件视图

```text
+------------------------------------------------------+
| Windows 11                                            |
|                                                      |
|  +------------------+      +----------------------+   |
|  | Windows Firewall | ---> | WSL2 Ubuntu          |   |
|  +------------------+      |                      |   |
|                            |  +----------------+  |   |
|                            |  | K3s Server     |  |   |
|                            |  | - apiserver    |  |   |
|                            |  | - scheduler    |  |   |
|                            |  | - controller   |  |   |
|                            |  | - containerd   |  |   |
|                            |  | - CoreDNS      |  |   |
|                            |  | - Traefik      |  |   |
|                            |  | - local-path   |  |   |
|                            |  +----------------+  |   |
|                            |          |           |   |
|                            |  +----------------+  |   |
|                            |  | App workloads   |  |   |
|                            |  +----------------+  |   |
|                            +----------------------+   |
+------------------------------------------------------+
```

### 5.2 网络入口

一期推荐入口：

```text
LAN Client
  -> http://<windows-lan-ip>
  -> Windows Firewall
  -> WSL2 mirrored network 或 portproxy
  -> K3s Traefik
  -> Kubernetes Service
  -> Pod
```

优先策略：

1. Windows 11 和 WSL 版本支持时，启用 WSL2 mirrored networking。
2. mirrored networking 不稳定或不可用时，使用 Windows `netsh interface portproxy` 把 Windows 的 80/443 转发到 WSL2 IP。
3. 如果只是临时验证，可以直接暴露 NodePort，但不作为长期入口。

端口规划：

| 端口 | 用途 | 一期建议 |
| --- | --- | --- |
| 80 | HTTP Ingress | 开启 |
| 443 | HTTPS Ingress | 预留，后续开启 |
| 6443 | Kubernetes API | 仅本机访问，不对局域网开放 |
| 30000-32767 | NodePort | 默认不开放 |

局域网域名：

- 初期：直接使用 `http://<windows-lan-ip>`。
- 稳定后：在路由器、AdGuard Home、Pi-hole 或本机 hosts 中配置 `jarvis.lan -> <windows-lan-ip>`。

### 5.3 Kubernetes 入口控制

一期使用 K3s 默认 Traefik：

- 减少组件数量。
- 与 K3s 默认安装集成。
- 足够支撑单机 Web/API 服务。

后续切换条件：

- 需要更精细的 NGINX 注解生态。
- 需要和云上 ingress-nginx 保持一致。
- 需要复用已有 NGINX Ingress 配置。

如果满足上述条件，再禁用默认 Traefik 并安装 ingress-nginx。

### 5.4 存储设计

一期使用 local-path-provisioner：

```text
PVC
  -> local-path StorageClass
  -> WSL2 Ubuntu 文件系统
  -> Windows 磁盘上的 WSL2 ext4.vhdx
```

使用原则：

- 数据库、向量库、对象存储等有状态服务必须使用 PVC。
- 重要数据必须有独立备份，不把 PVC 当成备份。
- 不建议把高频读写数据直接放在 `/mnt/c` 这类 Windows 挂载路径下。
- 优先使用 WSL2 Ubuntu 原生 ext4 文件系统路径。

备份策略：

- 一期最低要求：每天导出关键数据到 Windows 指定目录或外部盘。
- 对 SQLite/Postgres/向量库等服务分别使用应用级备份。
- 定期备份 Kubernetes manifests、Helm values、Secret 密文备份策略另行定义。

### 5.5 资源预算

宿主机内存 64GB，初期给 WSL2/Kubernetes 20GB。

建议 `.wslconfig`：

```ini
[wsl2]
memory=20GB
processors=8
swap=8GB
networkingMode=mirrored
```

一期资源预算：

| 类别 | 预算 | 说明 |
| --- | ---: | --- |
| WSL2 + K3s 系统组件 | 2-3GB | apiserver、containerd、CoreDNS、Traefik 等 |
| 基础中间件 | 3-5GB | 数据库、缓存、向量库、对象存储按需启用 |
| 业务服务 | 8-12GB | API、worker、前端、任务服务 |
| 预留缓冲 | 2-4GB | 镜像拉取、滚动发布、临时任务 |

资源约束原则：

- 每个 Deployment 必须配置 `resources.requests`。
- 关键服务必须配置 `resources.limits`。
- JVM、Node.js、Python worker 这类容易吃内存的服务必须单独限制。
- 单个普通业务服务一期不建议超过 4GB limit。
- 有状态服务的内存 limit 要结合实际压测，不做盲目压低。

示例：

```yaml
resources:
  requests:
    cpu: "250m"
    memory: "512Mi"
  limits:
    cpu: "2"
    memory: "2Gi"
```

## 6. 二期 GPU 架构设计

### 6.1 GPU 链路

二期目标链路：

```text
NVIDIA GPU
  -> Windows NVIDIA Driver
  -> WSL2 GPU support
  -> Ubuntu WSL2
  -> K3s containerd
  -> NVIDIA Container Toolkit
  -> NVIDIA Kubernetes device plugin / GPU Operator
  -> Pod requests nvidia.com/gpu
```

### 6.2 GPU 方案选型

推荐分两步：

1. **先使用 NVIDIA device plugin**：链路短，适合单机验证 GPU 调度。
2. **再评估 GPU Operator**：当需要 DCGM exporter、runtime 自动化、组件统一管理时引入。

GPU Operator 注意点：

- Windows 上驱动由 Windows NVIDIA Driver 提供。
- WSL2 内通常不应让 GPU Operator 安装 Linux kernel driver。
- 需要按宿主已有驱动的方式配置，例如关闭 driver 安装。

### 6.3 GPU 工作负载规范

GPU Pod 必须显式申请 GPU：

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

调度原则：

- GPU 服务单独 namespace，例如 `gpu-system`、`model-serving`。
- GPU 服务必须设置健康检查。
- 模型权重放在 PVC 或独立模型目录，不打进业务镜像。
- 大模型服务默认不和普通 API 混部，避免内存和显存争抢。
- GPU 服务需要独立的启动探针，避免模型加载慢导致反复重启。

### 6.4 二期资源调整

如果接入 GPU，20GB WSL2 内存可能仍可运行小模型或轻量推理，但需要重新分配：

| 类别 | 建议 |
| --- | --- |
| WSL2 memory | 从 20GB 评估提升到 32GB 或更多 |
| swap | 保持 8-16GB，但不依赖 swap 承载模型服务 |
| CPU | 至少 8 核，模型服务和业务服务分开限制 |
| 显存 | 按模型大小和并发单独预算 |

是否提高 WSL2 内存取决于：

- 模型权重是否需要同时加载到内存和显存。
- 是否运行向量库、重排模型、语音模型等多个服务。
- 是否存在批处理任务。

## 7. 部署分期

### 7.1 一期里程碑

M1：宿主环境准备

- 安装 WSL2 Ubuntu。
- 配置 `.wslconfig`。
- 启用 systemd。
- 验证 WSL2 网络模式。

M2：K3s 单节点集群

- 安装 K3s。
- 配置 kubeconfig。
- 验证 `kubectl get nodes`。
- 验证 CoreDNS、Traefik、local-path-provisioner。

M3：局域网入口

- 配置 mirrored networking 或 portproxy。
- Windows 防火墙放行 80。
- 部署 demo HTTP 服务。
- 同 Wi-Fi 手机或另一台电脑访问验证。

M4：应用发布基线

- 建立 namespace 规划。
- 建立基础 Deployment、Service、Ingress 模板。
- 建立 ConfigMap、Secret、PVC 使用规范。
- 建立镜像构建和发布流程。

M5：运维基线

- 整理启动、停止、重启、状态检查命令。
- 建立备份路径。
- 建立最小监控视图。
- 记录故障恢复步骤。

### 7.2 二期里程碑

G1：WSL2 GPU 验证

- Windows 安装 NVIDIA Driver。
- WSL2 Ubuntu 中验证 `nvidia-smi`。
- 验证容器内 GPU 可见。

G2：Kubernetes GPU 调度

- 配置 K3s containerd NVIDIA runtime。
- 安装 NVIDIA device plugin。
- 验证 `kubectl describe node` 中出现 `nvidia.com/gpu`。
- 运行 GPU demo Pod。

G3：模型服务化

- 部署一个 GPU 推理服务。
- 配置 Ingress 或内部 Service。
- 加入探针、资源限制、PVC。
- 压测显存、内存、冷启动时间。

G4：GPU 运维

- 接入 GPU 利用率、显存、温度监控。
- 梳理模型更新流程。
- 梳理 GPU 服务故障恢复流程。

## 8. Namespace 与服务规划

一期建议 namespace：

| Namespace | 用途 |
| --- | --- |
| `infra` | 基础中间件，如数据库、缓存、对象存储 |
| `apps` | 普通业务服务 |
| `observability` | 监控、日志、指标 |
| `sandbox` | 临时验证服务 |

二期新增：

| Namespace | 用途 |
| --- | --- |
| `gpu-system` | GPU device plugin / operator 相关组件 |
| `model-serving` | GPU 模型推理服务 |

## 9. 安全边界

一期安全原则：

- Kubernetes API Server 只允许本机访问，不暴露到局域网。
- 局域网只开放业务入口端口，默认 80，后续 443。
- 不在 Ingress 中暴露 dashboard、数据库、缓存管理端。
- Secret 不提交到 Git。
- 管理命令只在 WSL2 Ubuntu 中执行。
- Windows 防火墙按端口和网络类型放行，不做全开放。

如果后续需要公网访问，必须另起设计：

- HTTPS 证书。
- 认证网关。
- 反向代理或隧道。
- 访问审计。
- 暴力破解和限流策略。

## 10. 可观测性设计

一期最小可观测性：

- `kubectl get pods -A`
- `kubectl top nodes`
- `kubectl top pods -A`
- K3s 服务状态。
- Traefik 入口日志。
- 应用容器日志。

后续增强：

- metrics-server。
- Prometheus + Grafana。
- Loki 或轻量日志采集。
- 节点磁盘、内存、CPU、网络监控。
- 二期增加 GPU exporter。

## 11. 运维边界

### 11.1 启停

Windows 重启或睡眠恢复后，需要检查：

- WSL2 是否启动。
- K3s 服务是否运行。
- WSL2 IP 是否变化。
- portproxy 是否仍指向正确 WSL2 IP。
- Ingress 是否可从局域网访问。

如果使用 mirrored networking，重点检查防火墙和端口监听。

如果使用 portproxy，WSL2 IP 变化后需要刷新转发规则。

### 11.2 升级

升级顺序：

1. 备份应用数据和 manifests。
2. 升级 K3s 小版本。
3. 验证核心组件。
4. 验证 Ingress。
5. 验证业务服务。

二期有 GPU 后，升级还要额外验证：

- Windows NVIDIA Driver。
- WSL2 内 GPU 可见性。
- NVIDIA runtime。
- device plugin / GPU Operator。
- GPU demo Pod。

### 11.3 备份

必须备份：

- 应用数据库。
- 业务上传文件。
- 模型配置和索引数据。
- Kubernetes manifests / Helm values。
- Ingress 和域名配置。

不建议只备份：

- WSL2 整个虚拟磁盘。

原因是整盘备份可以作为灾难恢复手段，但不能替代应用级备份。应用级备份更容易验证、迁移和恢复单个服务。

## 12. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| WSL2 IP 变化 | portproxy 失效 | 优先 mirrored networking；否则写刷新脚本 |
| Windows 睡眠恢复异常 | 服务不可访问 | 设置状态检查脚本；关键服务避免依赖睡眠场景 |
| 本地盘损坏 | 数据丢失 | 应用级备份；重要数据外部盘或 NAS 备份 |
| 单机资源耗尽 | 服务不稳定 | 强制 requests/limits；监控内存和磁盘 |
| GPU runtime 配置复杂 | GPU Pod 不可调度 | 二期先 device plugin 验证，再引入 GPU Operator |
| 路由器隔离 Wi-Fi 客户端 | 同网设备不可访问 | 关闭 AP isolation；确认设备处于同一网段 |
| 防火墙未放行 | 局域网访问失败 | 明确入站规则，只放行必要端口 |

## 13. 验收标准

一期验收：

- `kubectl get nodes` 显示单节点 Ready。
- `kubectl get pods -A` 核心组件 Running。
- demo 服务可通过 Ingress 在 WSL2 内访问。
- demo 服务可通过 Windows 局域网 IP 从同 Wi-Fi 设备访问。
- 重启 K3s 后服务可恢复。
- Windows 重启后，有明确步骤恢复服务。
- 有一个 PVC demo 服务能读写持久化数据。
- 所有业务样例 Deployment 都有 requests/limits。

二期验收：

- WSL2 Ubuntu 中 `nvidia-smi` 可用。
- 容器内可见 GPU。
- Kubernetes Node 上出现 `nvidia.com/gpu` 资源。
- GPU demo Pod 成功运行。
- 一个模型推理服务能通过 Kubernetes 暴露调用。
- GPU 服务有资源声明、探针和基本监控。

## 14. 后续 Runbook 文档

本设计文档确定方向和边界。后续应补充独立 runbook：

1. `Windows + WSL2 + K3s 安装手册`
2. `K3s 局域网 Ingress 暴露手册`
3. `Kubernetes 应用发布模板`
4. `K3s 本地备份与恢复手册`
5. `WSL2 + K3s + NVIDIA GPU 接入手册`
