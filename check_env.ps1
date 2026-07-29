$ErrorActionPreference = "Continue"

Write-Host "=== Chrome Client build environment check ===" -ForegroundColor Cyan
$allOk = $true

foreach ($tool in @("rustc", "cargo", "rustup", "python", "maturin", "docker")) {
    if (Get-Command $tool -ErrorAction SilentlyContinue) {
        Write-Host "[OK] $tool" -ForegroundColor Green
    } else {
        Write-Host "[MISSING] $tool" -ForegroundColor Red
        $allOk = $false
    }
}

if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[NOT RUNNING] Docker" -ForegroundColor Red
        $allOk = $false
    }
}

if (Get-Command rustup -ErrorAction SilentlyContinue) {
    $installedTargets = rustup target list --installed
    foreach ($target in @(
        "x86_64-pc-windows-msvc",
        "x86_64-unknown-linux-gnu",
        "aarch64-apple-darwin"
    )) {
        if ($installedTargets -contains $target) {
            Write-Host "[OK] Rust target: $target" -ForegroundColor Green
        } else {
            Write-Host "[MISSING] Rust target: $target" -ForegroundColor Yellow
            $allOk = $false
        }
    }
}

foreach ($library in @(
    "cronet-libs\windows\cronet.144.0.7506.0.dll",
    "cronet-libs\linux\libcronet.144.0.7506.0.so",
    "cronet-libs\macos\libcronet.144.0.7506.0.dylib"
)) {
    if (Test-Path -LiteralPath $library) {
        Write-Host "[OK] $library" -ForegroundColor Green
    } else {
        Write-Host "[MISSING] $library" -ForegroundColor Red
        $allOk = $false
    }
}

$nssLibraries = @(Get-ChildItem -Path "linux_deps\*.so" -ErrorAction SilentlyContinue)
if ($nssLibraries.Count -ne 9) {
    Write-Host "[MISSING] Linux NSS libraries: $($nssLibraries.Count)/9" -ForegroundColor Red
    $allOk = $false
} else {
    Write-Host "[OK] Linux NSS libraries: 9/9" -ForegroundColor Green
}

if (-not $allOk) {
    exit 1
}

Write-Host "All required build components are available." -ForegroundColor Green
