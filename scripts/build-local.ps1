$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$pluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$manifestPath = Join-Path $pluginRoot "Cargo.toml"
$releaseBinary = Join-Path $pluginRoot "target\release\app-icon-toolkit-mcp.exe"
$binDirectory = Join-Path $pluginRoot "bin"
$installedBinary = Join-Path $binDirectory "app-icon-toolkit-mcp.exe"

cargo build --manifest-path $manifestPath --release --locked --package app-icon-mcp
New-Item -ItemType Directory -Force -Path $binDirectory | Out-Null
Copy-Item -Path $releaseBinary -Destination $installedBinary -Force
