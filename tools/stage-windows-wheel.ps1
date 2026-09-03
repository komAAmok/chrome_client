param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("windows-x86", "windows-x86_64", "windows-arm64")]
    [string]$Target,

    [ValidateSet("python", "python36")]
    [string]$Binding = "python"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$core = Join-Path $root "core\binaries\$Target"
$deps = Join-Path $root "core\dependencies\$Target"
# python36 reuses bindings/python as its python-source, so both bindings must
# stage runtime files into the same package directory.
$package = Join-Path $root "bindings\python\chrome_client"
$manifestPath = Join-Path $core "manifest.json"

foreach ($path in @(
    (Join-Path $core "minicronet.dll"),
    (Join-Path $core "minicronet.lib"),
    $manifestPath
)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing Windows Core artifact: $path"
    }
}

$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.target -ne $Target -or $manifest.library -ne "minicronet.dll") {
    throw "Core manifest does not match target $Target"
}
if ($manifest.runtime_dependencies.Count -ne 0) {
    throw "$Target manifest declares runtime dependencies; the ICU dataset is embedded"
}

$dll = Join-Path $core "minicronet.dll"
$dllHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $dll).Hash.ToLowerInvariant()
if ($dllHash -ne $manifest.sha256) {
    throw "$Target minicronet.dll SHA-256 mismatch"
}

New-Item -ItemType Directory -Force -Path $package | Out-Null
Copy-Item -Force -LiteralPath $dll -Destination $package
Write-Host "Staged minicronet.dll in $package"
