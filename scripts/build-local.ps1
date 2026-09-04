param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($args.Count -ne 0) {
    throw "Usage: build-local.ps1"
}

$pluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$manifestPath = Join-Path $pluginRoot "Cargo.toml"
$targetParent = Join-Path $pluginRoot "target"
$buildTarget = Join-Path $targetParent ("build-local-" + [Guid]::NewGuid().ToString("N"))
$releaseBinary = Join-Path $buildTarget "release\app-icon-toolkit-mcp.exe"
$binDirectory = Join-Path $pluginRoot "bin"
$installedBinary = Join-Path $binDirectory "app-icon-toolkit-mcp.exe"
$locationPushed = $false

New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
New-Item -ItemType Directory -Path $buildTarget | Out-Null
try {
    Push-Location $pluginRoot
    $locationPushed = $true
    & cargo build --manifest-path $manifestPath --release --locked `
        --package app-icon-mcp --target-dir $buildTarget
    if ($LASTEXITCODE -ne 0) {
        throw "Cargo failed to build the local plugin (exit code $LASTEXITCODE)."
    }

    $releaseItem = Get-Item -LiteralPath $releaseBinary -ErrorAction Stop
    $isReparsePoint = 0 -ne (
        $releaseItem.Attributes -band [IO.FileAttributes]::ReparsePoint
    )
    if (
        $releaseItem.PSIsContainer -or
        $releaseItem.Length -le 0 -or
        $isReparsePoint
    ) {
        throw "Cargo did not produce a regular local plugin binary: $releaseBinary"
    }

    New-Item -ItemType Directory -Force -Path $binDirectory | Out-Null
    Copy-Item -LiteralPath $releaseBinary -Destination $installedBinary -Force
}
finally {
    if ($locationPushed) {
        Pop-Location
    }
    if (Test-Path -LiteralPath $buildTarget) {
        Remove-Item -LiteralPath $buildTarget -Recurse -Force
    }
}
