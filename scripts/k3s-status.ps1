$ErrorActionPreference = "Stop"

kubectl --context k3s-ubuntu-f get nodes -o wide
kubectl --context k3s-ubuntu-f get pods -A -o wide
kubectl --context k3s-ubuntu-f get storageclass
