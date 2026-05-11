# Windows WSL2 K3s 本地启动与维护手册

日期：2026-05-11

## 1. 当前环境

K3s 当前部署在 Windows 的 WSL2 发行版 `Ubuntu-F` 中。

当前约定：

- WSL 发行版：`Ubuntu-F`
- K3s 节点名：`ryan-pc`
- Kubernetes context：`k3s-ubuntu-f`
- WSL2 资源限制：12GB 内存、6 CPU、4GB swap
- K3s node IP：`192.168.31.211`
- Flannel 网卡：`eth0`
- 默认 StorageClass：`local-path`
- Ingress Controller：K3s 默认 Traefik

相关文件：

- Windows WSL 配置：`C:\Users\Administrator\.wslconfig`
- K3s 配置：`/etc/rancher/k3s/config.yaml`
- K3s systemd 环境：`/etc/systemd/system/k3s.service.env`
- Windows kubeconfig：`C:\Users\Administrator\.kube\config`
- 仓库启动脚本：`scripts/k3s-start.ps1`
- 仓库状态脚本：`scripts/k3s-status.ps1`

## 2. 日常启动

进入仓库目录：

```powershell
cd E:\pythonProject\jarvis
```

启动 WSL 常驻进程并等待 K3s Ready：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\k3s-start.ps1
```

确认状态：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\k3s-status.ps1
```

也可以直接执行：

```powershell
kubectl get nodes -o wide
kubectl get pods -A -o wide
kubectl top nodes
kubectl top pods -A
```

预期结果：

- `ryan-pc` 为 `Ready`
- `kube-system` 下 CoreDNS、Traefik、metrics-server、local-path-provisioner 均为 `Running`
- `kubectl top nodes` 能返回 CPU 和内存指标

## 3. 为什么需要 k3s-start.ps1

WSL2 发行版如果没有前台或后台 Linux 进程，可能会自动退出。

K3s 是运行在 `Ubuntu-F` 里的 systemd 服务。如果 `Ubuntu-F` 被 WSL 回收，K3s 也会停止。

`scripts/k3s-start.ps1` 会启动一个隐藏的常驻进程：

```text
wsl -d Ubuntu-F -- sleep infinity
```

这样 `Ubuntu-F` 会保持运行，K3s 服务也会持续运行。

## 4. 当前 K3s 配置

查看 K3s 配置：

```powershell
wsl -d Ubuntu-F -- cat /etc/rancher/k3s/config.yaml
```

当前应类似：

```yaml
write-kubeconfig-mode: '0644'
node-ip: 192.168.31.211
node-external-ip: 192.168.31.211
tls-san:
  - 127.0.0.1
  - 192.168.31.211
flannel-iface: eth0
```

关键点：

- `node-ip` 固定为 WSL 的真实局域网地址，避免使用 WSL mirrored 的 `198.18.0.1` 虚拟接口。
- `node-external-ip` 与 `node-ip` 保持一致，便于后续局域网暴露。
- `flannel-iface: eth0` 强制 Pod 网络使用 Wi-Fi/局域网对应的 WSL 网卡。
- `tls-san` 包含 `127.0.0.1` 和当前节点 IP，避免 kubeconfig/API 证书访问问题。

## 5. IP 变化处理

如果更换 Wi-Fi、路由器重新分配地址，`192.168.31.211` 可能会变化。

先查看当前 WSL 的 eth0 地址：

```powershell
wsl -d Ubuntu-F -- ip -4 addr show eth0
```

输出中类似：

```text
inet 192.168.31.211/24
```

如果地址变成了新的 `192.168.x.x`，需要更新 K3s 配置。

编辑配置：

```powershell
wsl -d Ubuntu-F -- nano /etc/rancher/k3s/config.yaml
```

把以下字段改成新 IP：

```yaml
node-ip: 新IP
node-external-ip: 新IP
tls-san:
  - 127.0.0.1
  - 新IP
flannel-iface: eth0
```

重启 K3s：

```powershell
wsl -d Ubuntu-F -- systemctl restart k3s
```

如果 `kubectl get nodes -o wide` 仍显示旧 IP，需要删除旧 Node 对象，让 kubelet 重新注册：

```powershell
kubectl delete node ryan-pc
powershell -ExecutionPolicy Bypass -File scripts\k3s-start.ps1
```

重新确认：

```powershell
kubectl get nodes -o wide
kubectl get pods -A -o wide
kubectl top nodes
```

## 6. 代理配置

K3s systemd 环境当前配置了代理和 NO_PROXY：

```powershell
wsl -d Ubuntu-F -- cat /etc/systemd/system/k3s.service.env
```

重点是 `NO_PROXY` 必须覆盖：

- `127.0.0.1`
- `10.42.0.0/16`
- `10.43.0.0/16`
- `192.168.0.0/16`
- `198.18.0.0/15`
- `.svc`
- `.cluster.local`

原因：

- `10.42.0.0/16` 是 Pod 网段。
- `10.43.0.0/16` 是 Service 网段。
- `192.168.0.0/16` 是局域网网段。
- `198.18.0.0/15` 是 WSL mirrored 网络可能出现的虚拟地址段。

如果没有这些 NO_PROXY，`kubectl logs`、`kubectl exec`、metrics-server 到 kubelet 的访问可能被代理转发，出现 `EOF`、`401`、timeout 或 metrics 不可用。

修改后需要执行：

```powershell
wsl -d Ubuntu-F -- systemctl daemon-reload
wsl -d Ubuntu-F -- systemctl restart k3s
```

## 7. 常见检查命令

节点：

```powershell
kubectl get nodes -o wide
kubectl describe node ryan-pc
```

Pod：

```powershell
kubectl get pods -A -o wide
kubectl describe pod -n kube-system <pod-name>
```

日志：

```powershell
kubectl logs -n kube-system deploy/metrics-server --since=10m
kubectl logs -n k3s-demo deploy/whoami --tail=20
```

指标：

```powershell
kubectl top nodes
kubectl top pods -A
```

K3s 服务：

```powershell
wsl -d Ubuntu-F -- systemctl status k3s --no-pager -l
wsl -d Ubuntu-F -- journalctl -u k3s --since "10 minutes ago" --no-pager
```

WSL IP 和路由：

```powershell
wsl -d Ubuntu-F -- ip -4 addr show
wsl -d Ubuntu-F -- ip route
```

## 8. 常见现象

### 8.1 metrics-server 日志出现 no metrics to serve

示例：

```text
Failed probe probe="metric-storage-ready" err="no metrics to serve"
```

如果只出现在 metrics-server 刚启动后的几十秒内，可以忽略。

判断是否已恢复：

```powershell
kubectl top nodes
kubectl top pods -A
kubectl logs -n kube-system deploy/metrics-server --since=60s
```

如果 `kubectl top` 正常，且最近 60 秒没有持续报错，则无须处理。

### 8.2 metrics-server 日志出现 apiserver not ready

示例：

```text
Failed to watch err="apiserver not ready"
```

这通常发生在 K3s 刚重启时。

判断是否需要处理：

```powershell
wsl -d Ubuntu-F -- systemctl status k3s --no-pager -l
kubectl get pods -A
kubectl top nodes
kubectl logs -n kube-system deploy/metrics-server --since=5m
```

如果只在 K3s 启动时间点附近出现，且之后不再持续出现，可以忽略。

### 8.3 kubectl logs 返回 EOF

示例：

```text
Error from server: Get "https://<node-ip>:10250/containerLogs/...": EOF
```

优先检查 K3s 的 `NO_PROXY`：

```powershell
wsl -d Ubuntu-F -- cat /etc/systemd/system/k3s.service.env
```

确保 `NO_PROXY` 覆盖 Node IP、Pod 网段和 Service 网段。

然后重启：

```powershell
wsl -d Ubuntu-F -- systemctl daemon-reload
wsl -d Ubuntu-F -- systemctl restart k3s
```

### 8.4 kubectl top 间歇性失败

检查 Node IP：

```powershell
kubectl get nodes -o wide
```

如果 `INTERNAL-IP` 是 `198.18.0.1`，应改为 WSL 的真实 `eth0` 地址，例如 `192.168.x.x`。

处理方式见“IP 变化处理”。

## 9. 停止集群

临时停止 K3s：

```powershell
wsl -d Ubuntu-F -- systemctl stop k3s
```

关闭整个 WSL：

```powershell
wsl --shutdown
```

注意：

- `wsl --shutdown` 会停止所有 WSL 发行版，包括 Docker Desktop 相关 WSL 后端。
- 停止后再次使用 K3s，需要重新运行 `scripts/k3s-start.ps1`。

## 10. 后续优化

建议后续把以下逻辑加入 `scripts/k3s-start.ps1`：

1. 自动读取 `eth0` 当前 IPv4。
2. 自动对比 `/etc/rancher/k3s/config.yaml` 中的 `node-ip`。
3. IP 变化时自动更新 K3s 配置。
4. 必要时自动删除旧 Node 对象并等待重新注册。
5. 自动检查 metrics-server 是否稳定返回 `kubectl top`。
