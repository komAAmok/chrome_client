# chrome_client

基于 Chromium 网络栈的高稳定 HTTP/WebSocket 请求库。项目使用已经编译好的
`libminicronet` Core，语言绑定不重新实现 TLS、HTTP 或 WebSocket，也不需要重新编译
Chromium。

## 架构

```text
Python (requests / Client / Session / AsyncClient / AsyncSession)
Go / Node.js / Rust
                 │ 薄绑定：类型转换、回调与生命周期
                 ▼
        安全 Rust 层：配置校验、所有权、错误与 ABI 边界
                 │ minicronet-sys
                 ▼
       C ABI (ABI v7) ── libminicronet Core ── Chromium 网络栈
                              │
             TLS · HTTP/1.1 · HTTP/2 · HTTP/3/QUIC
                         WebSocket/WSS · Proxy
```

Core 是唯一的协议实现；绑定层只负责语言适配。平台 Core 二进制按目标架构选择，
每个产物都带有 ABI、Chromium revision、目标三元组、SHA-256 和运行时依赖清单。

## 主要优势

- **浏览器网络行为**：复用 Chromium/BoringSSL，支持 HTTP/1.1、HTTP/2、HTTP/3/QUIC
  和 WebSocket/WSS；可使用匹配 Core 的 `impersonate` profile。
- **同步与异步**：Python 提供 requests 风格 API 和 `Client`、`Session`、
  `AsyncClient`、`AsyncSession`；异步由 Core 回调唤醒事件循环，不为每个请求创建线程池。
- **高并发与背压**：请求生命周期由 Rust 管理，支持流式响应、响应大小限制、取消、
  超时和有界 WebSocket 队列，避免 Python GIL 或无界缓存拖垮进程。
- **兼容性**：主 Python wheel 支持 3.7–3.13；独立 Python 3.6 wheel 使用 `abi3`。
  支持 Windows、Linux（manylinux2014/glibc 2.17+）和 macOS 的 x86、x86_64、ARM64。
- **可部署**：绑定与 Core 解耦，使用仓库中现有二进制即可构建 wheel；Windows 随包携带
  必需的 ICU 数据，Linux/macOS 按平台收集动态依赖。

## Python 示例

```python
from chrome_client import requests

response = requests.get(
    "https://example.com",
    impersonate="chrome_151",
    proxies={"https": "http://127.0.0.1:8080"},
    timeout=10,
)
response.raise_for_status()
print(response.status_code, response.text)
```

异步请求不使用请求线程池：

```python
import asyncio
from chrome_client import AsyncClient

async def main():
    async with AsyncClient(impersonate="chrome_151") as client:
        response = await client.get("https://example.com")
        print(response.status_code)

asyncio.run(main())
```

## 目录

- `core/abi/`：稳定 C ABI 合约
- `core/binaries/`：已审计的各平台 Core 二进制
- `crates/minicronet/`：安全 Rust API 与资源生命周期
- `crates/minicronet-sys/`：原始 FFI 与目标平台链接选择
- `bindings/`：Python、Go、Node.js 绑定
- `docs/`：迁移、架构、ABI、兼容性、wheel 和 Python 审计文档

构建与兼容边界见 [`docs/BUILD.md`](docs/BUILD.md)、[`docs/PLATFORM_SUPPORT.md`](docs/PLATFORM_SUPPORT.md)
和 [`docs/COMPATIBILITY_BOUNDARY.md`](docs/COMPATIBILITY_BOUNDARY.md)。
