$ErrorActionPreference = "Stop"

$distro = "Ubuntu-F"

Start-Process -FilePath "wsl.exe" -ArgumentList @("-d", $distro, "--", "sleep", "infinity") -WindowStyle Hidden | Out-Null
Start-Sleep -Seconds 2

for ($i = 0; $i -lt 45; $i++) {
    $nodeOutput = wsl -d $distro -- kubectl get nodes --no-headers 2>$null
    if ($LASTEXITCODE -eq 0 -and $nodeOutput -match " Ready ") {
        kubectl --context k3s-ubuntu-f get nodes -o wide
        exit 0
    }

    Start-Sleep -Seconds 2
}

wsl -d $distro -- systemctl status k3s --no-pager -l
exit 1
