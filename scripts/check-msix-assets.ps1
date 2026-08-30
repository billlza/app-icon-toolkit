param(
    [Parameter(Mandatory = $true)]
    [string]$Target,

    [Parameter(Mandatory = $true)]
    [string]$Toolchain,

    [Parameter(Mandatory = $true)]
    [string]$Binary
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)] [string]$Command,
        [Parameter(Mandatory = $true)] [string[]]$Arguments,
        [bool]$RejectWarnings = $false
    )

    $lines = & $Command @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($lines) {
        $lines | ForEach-Object { Write-Host $_ }
    }
    if ($exitCode -ne 0) {
        throw "command failed with exit code ${exitCode}: $Command"
    }
    if ($RejectWarnings -and (($lines | Out-String) -match "(?im)(^|\s)warning([:\s]|$)")) {
        throw "validator emitted a warning: $Command"
    }
}

function Find-WindowsSdkTool {
    param([Parameter(Mandatory = $true)] [string]$Name)

    $kitsRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    $versions = Get-ChildItem -LiteralPath $kitsRoot -Directory |
        Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } -Descending
    foreach ($version in $versions) {
        $candidate = Join-Path $version.FullName "x64\$Name"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    throw "$Name was not found in an installed Windows 10 SDK x64 tool directory"
}

$pluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$binaryPath = (Resolve-Path -LiteralPath $Binary).Path
$targetDetailsOutput = & python (
    Join-Path $pluginRoot "scripts\release_targets.py"
) --contract (
    Join-Path $pluginRoot "scripts\release-targets.json"
) target-details --target $Target 2>&1
$targetDetailsExitCode = $LASTEXITCODE
if ($targetDetailsOutput) {
    $targetDetailsOutput | ForEach-Object { Write-Host $_ }
}
if ($targetDetailsExitCode -ne 0) {
    throw "release target contract lookup failed with exit code ${targetDetailsExitCode}: $Target"
}
$targetDetails = ($targetDetailsOutput | Out-String) | ConvertFrom-Json
if ($targetDetails.family -ne "windows_msvc") {
    throw "MSIX validation requires a windows_msvc release target: $Target"
}
$architecture = switch ($targetDetails.test_target) {
    "aarch64-pc-windows-msvc" { "arm64" }
    "x86_64-pc-windows-msvc" { "x64" }
    default { throw "unsupported MSIX manifest architecture: $($targetDetails.test_target)" }
}
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "app-icon-toolkit-msix-" + [Guid]::NewGuid().ToString("N")
)

New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
$validationError = $null
try {
    $workspace = Join-Path $temporaryRoot "workspace"
    Invoke-Checked -Command "cargo" -Arguments @(
        "+$Toolchain",
        "run",
        "--quiet",
        "--locked",
        "--package",
        "app-icon-engine",
        "--example",
        "validation_fixture",
        "--target",
        $Target,
        "--",
        $workspace
    )

    $packageRoot = Join-Path $temporaryRoot "package"
    $assets = Join-Path $packageRoot "Assets"
    New-Item -ItemType Directory -Path $assets | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $workspace "generated\windows\msix\Assets") -File |
        Copy-Item -Destination $assets
    Copy-Item -LiteralPath $binaryPath -Destination (Join-Path $packageRoot "App.exe")

    $manifestTemplate = Get-Content -Raw -LiteralPath (
        Join-Path $pluginRoot "crates\app-icon-engine\tests\fixtures\AppxManifest.xml"
    )
    $manifest = $manifestTemplate.Replace("__ARCHITECTURE__", $architecture)
    Set-Content -LiteralPath (Join-Path $packageRoot "AppxManifest.xml") `
        -Value $manifest -Encoding utf8NoBOM

    $makePri = Find-WindowsSdkTool -Name "makepri.exe"
    $makeAppx = Find-WindowsSdkTool -Name "makeappx.exe"
    $priConfig = Join-Path $temporaryRoot "priconfig.xml"
    Invoke-Checked -Command $makePri -Arguments @(
        "createconfig", "/cf", $priConfig, "/dq", "en-US"
    ) -RejectWarnings $true
    Invoke-Checked -Command $makePri -Arguments @(
        "new",
        "/pr", $packageRoot,
        "/cf", $priConfig,
        "/mn", (Join-Path $packageRoot "AppxManifest.xml"),
        "/of", (Join-Path $packageRoot "resources.pri")
    ) -RejectWarnings $true

    $package = Join-Path $temporaryRoot "validation.msix"
    Invoke-Checked -Command $makeAppx -Arguments @(
        "pack", "/d", $packageRoot, "/p", $package
    ) -RejectWarnings $true
    $packageInfo = Get-Item -LiteralPath $package
    if ($packageInfo.Length -le 0) {
        throw "MakeAppx produced an empty package"
    }
}
catch {
    $validationError = $_
}

$cleanupError = $null
try {
    $expectedPrefix = Join-Path ([IO.Path]::GetTempPath()) "app-icon-toolkit-msix-"
    if ($temporaryRoot.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
    else {
        throw "refusing to remove unexpected temporary path: $temporaryRoot"
    }
}
catch {
    $cleanupError = $_
}

if ($null -ne $validationError -and $null -ne $cleanupError) {
    throw [AggregateException]::new(
        "MSIX validation and temporary cleanup both failed",
        [Exception[]]@($validationError.Exception, $cleanupError.Exception)
    )
}
if ($null -ne $validationError) {
    throw $validationError
}
if ($null -ne $cleanupError) {
    throw $cleanupError
}
