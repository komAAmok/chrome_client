# 编译所有平台的 wheel
param(
    [ValidateSet("windows", "windows32", "linux", "macos", "all")]
    [string]$Platform = "all"
)

$ErrorActionPreference = "Stop"

function Normalize-WheelTags([string]$Pattern) {
    python -m pip install --disable-pip-version-check wheel==0.45.1
    Get-ChildItem $Pattern | ForEach-Object {
        python -m wheel tags --python-tag py3 --abi-tag none --remove $_.FullName
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}
$ProjectDir = $PSScriptRoot  # 使用脚本所在目录

function Build-Windows {
    Write-Host "`n=== 编译 Windows x86_64 Wheel ===" -ForegroundColor Cyan
    cd $ProjectDir

    Write-Host "清理其他平台的库文件..." -ForegroundColor Yellow
    Remove-Item python\chrome_client\*.so -ErrorAction SilentlyContinue
    Remove-Item python\chrome_client\*.dylib -ErrorAction SilentlyContinue

    Write-Host "复制 Windows x64 DLL..." -ForegroundColor Yellow
    Copy-Item cronet-libs\windows\cronet.144.0.7506.0.dll python\chrome_client\ -Force

    Write-Host "开始构建..." -ForegroundColor Green
    maturin build --release --locked

    if ($LASTEXITCODE -eq 0) {
        Normalize-WheelTags "$ProjectDir\target\wheels\*.whl"
        Write-Host "✓ Windows x64 wheel 构建成功" -ForegroundColor Green
    } else {
        Write-Host "✗ Windows x64 wheel 构建失败" -ForegroundColor Red
        exit 1
    }
}

function Build-Windows32 {
    Write-Host "`n=== Building Windows x86 Wheel ===" -ForegroundColor Cyan
    Set-Location $ProjectDir

    Remove-Item python\chrome_client\*.so -ErrorAction SilentlyContinue
    Remove-Item python\chrome_client\*.dylib -ErrorAction SilentlyContinue
    Copy-Item cronet-libs\windows32\cronet.144.0.7506.0.dll python\chrome_client\ -Force

    rustup target add i686-pc-windows-msvc
    maturin build --release --locked --target i686-pc-windows-msvc
    if ($LASTEXITCODE -ne 0) {
        exit 1
    }
    Normalize-WheelTags "$ProjectDir\target\wheels\*.whl"
}

function Build-Linux {
    Write-Host "`n=== 编译 Linux x86_64 Wheel (manylinux_2_18) ===" -ForegroundColor Cyan
    cd $ProjectDir

    Write-Host "清理其他平台的库文件..." -ForegroundColor Yellow
    Remove-Item python\chrome_client\*.dll -ErrorAction SilentlyContinue
    Remove-Item python\chrome_client\*.dylib -ErrorAction SilentlyContinue

    Write-Host "复制 Linux SO 和 NSS 依赖..." -ForegroundColor Yellow
    Copy-Item cronet-libs\linux\libcronet.144.0.7506.0.so python\chrome_client\ -Force
    Copy-Item linux_deps\*.so python\chrome_client\ -Force

    Write-Host "检查 Docker..." -ForegroundColor Yellow
    try {
        docker version | Out-Null
    } catch {
        Write-Host "✗ Docker 未运行，请启动 Docker Desktop" -ForegroundColor Red
        exit 1
    }

    Write-Host "开始构建（使用 Docker）..." -ForegroundColor Green
    $mountPath = $ProjectDir -replace '\\', '/'
    docker run --rm `
        -v "${mountPath}:/io" `
        -e LD_LIBRARY_PATH=/io/python/chrome_client `
        ghcr.io/pyo3/maturin:latest `
        build --release --locked --target x86_64-unknown-linux-gnu --compatibility linux `
        --interpreter /opt/python/cp310-cp310/bin/python3.10

    if ($LASTEXITCODE -eq 0) {
        python -m pip install --disable-pip-version-check wheel==0.45.1
        Get-ChildItem "$ProjectDir\target\wheels\*-linux_x86_64.whl" |
            ForEach-Object {
                python -m wheel tags --python-tag py3 --abi-tag none --platform-tag manylinux_2_18_x86_64 --remove $_.FullName
                if ($LASTEXITCODE -ne 0) {
                    exit 1
                }
            }
        Write-Host "✓ Linux wheel 构建成功" -ForegroundColor Green
    } else {
        Write-Host "✗ Linux wheel 构建失败" -ForegroundColor Red
        exit 1
    }
}

function Build-MacOS {
    Write-Host "`n=== 编译 macOS ARM64 Wheel ===" -ForegroundColor Cyan
    cd $ProjectDir

    Write-Host "清理其他平台的库文件..." -ForegroundColor Yellow
    Remove-Item python\chrome_client\*.dll -ErrorAction SilentlyContinue
    Remove-Item python\chrome_client\*.so -ErrorAction SilentlyContinue

    Write-Host "复制 macOS dylib..." -ForegroundColor Yellow
    Copy-Item cronet-libs\macos\libcronet.144.0.7506.0.dylib python\chrome_client\ -Force
    Copy-Item cronet-bin\mac\libcronet.dylib libcronet.144.0.7506.0.dylib -Force

    Write-Host "检查 Rust 目标..." -ForegroundColor Yellow
    $targets = rustup target list --installed
    if ($targets -notcontains "aarch64-apple-darwin") {
        Write-Host "安装 aarch64-apple-darwin 目标..." -ForegroundColor Yellow
        rustup target add aarch64-apple-darwin
    }

    Write-Host "开始构建（使用 zig 交叉编译）..." -ForegroundColor Green
    maturin build --release --locked --target aarch64-apple-darwin --zig

    if ($LASTEXITCODE -eq 0) {
        Normalize-WheelTags "$ProjectDir\target\wheels\*.whl"
        Write-Host "✓ macOS wheel 构建成功" -ForegroundColor Green
    } else {
        Write-Host "✗ macOS wheel 构建失败" -ForegroundColor Red
        exit 1
    }
}

# 主逻辑
Write-Host "=== Chrome Client 多平台 Wheel 构建工具 ===" -ForegroundColor Cyan
Write-Host "项目目录: $ProjectDir" -ForegroundColor White

switch ($Platform) {
    "windows" { Build-Windows }
    "windows32" { Build-Windows32 }
    "linux" { Build-Linux }
    "macos" { Build-MacOS }
    "all" {
        Build-Windows
        Build-Windows32
        Build-Linux
        Build-MacOS
    }
}

# 显示构建结果
Write-Host "`n=== 构建完成 ===" -ForegroundColor Green
Write-Host "`n最近构建的 Wheel 文件:" -ForegroundColor Cyan

Get-ChildItem "$ProjectDir\target\wheels\*.whl" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 10 |
    ForEach-Object {
        $sizeMB = [math]::Round($_.Length / 1MB, 2)
        $time = $_.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        Write-Host "  $($_.Name)" -ForegroundColor White
        Write-Host "    大小: $sizeMB MB | 时间: $time" -ForegroundColor Gray
    }

Write-Host "`n下一步:" -ForegroundColor Cyan
Write-Host "  1. 测试 wheel: pip install target\wheels\chrome_client-*.whl" -ForegroundColor White
Write-Host "  2. 上传到 PyPI: python -m twine upload target\wheels\chrome_client-*.whl" -ForegroundColor White
