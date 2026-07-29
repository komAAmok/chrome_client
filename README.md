# chrome_client

基于 Chromium Cronet 和 Rust/PyO3 的 Python HTTP 客户端，提供同步、异步、流式请求、Cookie、代理和可配置 TLS Profile。

> Python 导入名和 PyPI 项目名均为 `chrome_client`。

## 功能

- 同步 API：`get()`、`post()`、`put()`、`delete()`、`patch()`、`head()`、`options()`
- 异步 API：`async_get()`、`async_post()` 等
- 可复用的 `CronetClient` / `AsyncCronetClient` 会话
- HTTP、HTTPS、SOCKS5 和 SOCKS5H 代理
- Cookie 自动保存、发送、查询和删除
- 重定向、超时、证书验证和流式响应
- 有序请求头
- 内置及自定义 TLS Profile
- Rust 原生扩展，使用 PyO3 `abi3-py36`

## 兼容性

| 平台 | 架构 | 状态 |
|---|---|---|
| Windows | x86_64 | 支持 |
| Windows | x86（32 位） | 支持 |
| Linux | x86_64，glibc >= 2.24 | 支持 |
| macOS | Apple Silicon / arm64 | 支持 |

- Python：`>= 3.6`
- Linux Wheel：`manylinux_2_24_x86_64`
- Ubuntu：支持 Ubuntu 20.04 及以上的 x86_64 系统
- 当前不支持 Linux ARM64、macOS Intel 和 Alpine Linux（musl）

## 安装

发布到 PyPI 后：

```bash
python -m pip install chrome_client
```

升级：

```bash
python -m pip install --upgrade chrome_client
```

## 快速开始

### 单次同步请求

```python
import chrome_client

response = chrome_client.get("https://example.com")
response.raise_for_status()

print(response.status_code)
print(response.headers)
print(response.text)
```

### 同步 Session

```python
import chrome_client

with chrome_client.CronetClient(
    verify=True,
    timeout_ms=30000,
    chrometls="chrome_150",
) as client:
    response = client.get(
        "https://example.com/api",
        params={"page": 1},
        headers={"accept": "application/json"},
    )
    print(response.json())
```

### POST JSON

```python
import chrome_client

response = chrome_client.post(
    "https://example.com/api",
    json={"name": "chrome_client"},
    timeout=30,
)

print(response.status_code)
print(response.json())
```

### 异步请求

下面的写法兼容 Python 3.6：

```python
import asyncio
import chrome_client


async def main():
    async with chrome_client.AsyncCronetClient() as client:
        responses = await asyncio.gather(
            client.get("https://example.com/1"),
            client.get("https://example.com/2"),
        )
        for response in responses:
            print(response.status_code)


loop = asyncio.get_event_loop()
loop.run_until_complete(main())
```

Python 3.7 及以上也可以使用 `asyncio.run(main())`。

## 常用配置

### 代理

代理可以使用字符串或字典：

```python
import chrome_client

client = chrome_client.CronetClient(
    proxies="http://127.0.0.1:8080"
)

# 也可以使用：
# proxies={"https": "http://user:password@127.0.0.1:8080"}
# proxies="socks5://127.0.0.1:1080"
# proxies="socks5h://user:password@127.0.0.1:1080"

response = client.get("https://example.com")
client.close()
```

### 证书验证

证书验证默认启用：

```python
client = chrome_client.CronetClient(verify=True)
```

仅在明确需要访问测试环境或自签名服务时关闭：

```python
client = chrome_client.CronetClient(verify=False)
```

`verify` 在 Session 创建时确定；请求方法中的同名参数仅用于兼容常见 HTTP 客户端接口。

### 超时

Session 使用毫秒：

```python
client = chrome_client.CronetClient(timeout_ms=15000)
```

模块级请求使用秒：

```python
response = chrome_client.get("https://example.com", timeout=15)
```

### 有序请求头

需要严格控制顺序时使用元组列表：

```python
headers = [
    ("user-agent", "Mozilla/5.0"),
    ("accept", "text/html,application/xhtml+xml"),
    ("accept-language", "zh-CN,zh;q=0.9"),
]

response = chrome_client.get(
    "https://example.com",
    headers=headers,
)
```

### Cookie

```python
import chrome_client

with chrome_client.CronetClient(default_domain="example.com") as client:
    client.cookies.set("session", "value", domain="example.com")

    response = client.get("https://example.com/account")

    print(client.cookies.get("session", domain="example.com"))
    print(client.cookies.get_dict(domain="example.com"))

    client.cookies.delete("session", domain="example.com")
```

响应中的 `Set-Cookie` 会自动更新当前 Session 的 CookieJar。

## TLS Profile

默认 Profile 是 `chrome_150`。当前内置配置可通过代码查看：

```python
import chrome_client

print(sorted(chrome_client.get_tls_profiles().keys()))
```

选择 Profile：

```python
client = chrome_client.CronetClient(chrometls="chrome_150")
```

增加自定义 Profile：

```python
import chrome_client

profile = chrome_client.get_tls_profiles()["chrome_150"].copy()
profile["tls_curves"] = ["X25519MLKEM768", "X25519", "P-256", "P-384"]

chrome_client.add_tls_profile("chrome_custom", profile)

with chrome_client.CronetClient(chrometls="chrome_custom") as client:
    response = client.get("https://example.com")
    print(response.status_code)
```

相关函数：

- `get_tls_profiles()`：获取当前配置
- `add_tls_profile(name, profile)`：新增或更新一个配置
- `set_tls_profiles(profiles)`：替换当前进程中的全部配置
- `clear_tls_profiles_cache()`：清除缓存并在下次使用时重新读取文件

`add_tls_profile()` 和 `set_tls_profiles()` 只修改当前 Python 进程中的配置。需要持久化时，请修改 `python/chrome_client/tls_profiles.json` 后重新构建 Wheel。

## 流式响应

### 同步

```python
import chrome_client

response = chrome_client.get("https://example.com/file", stream=True)
try:
    with open("download.bin", "wb") as output:
        for chunk in response.iter_content(64 * 1024):
            output.write(chunk)
finally:
    response.close()
```

### 异步

```python
import asyncio
import chrome_client


async def download():
    async with chrome_client.AsyncCronetClient() as client:
        response = await client.get("https://example.com/file", stream=True)
        try:
            with open("download.bin", "wb") as output:
                async for chunk in response.aiter_content(64 * 1024):
                    output.write(chunk)
        finally:
            response.close()


loop = asyncio.get_event_loop()
loop.run_until_complete(download())
```

## Response

常用属性和方法：

```python
response.status_code
response.headers
response.cookies
response.content
response.text
response.json()
response.ok
response.raise_for_status()
```

异常类型：

```python
from chrome_client import HTTPStatusError, RequestError
```

## Linux 动态库排查

官方 Wheel 会携带所需的 Cronet 运行库。如果源码安装后出现：

```text
libcronet.144.0.7506.0.so: cannot open shared object file
```

可以临时将包目录加入动态库搜索路径：

```bash
export LD_LIBRARY_PATH="$(python -c 'import os, chrome_client; print(os.path.dirname(chrome_client.__file__))'):$LD_LIBRARY_PATH"
```

如果使用的是 Alpine Linux，请改用 glibc 系发行版；当前 Wheel 不是 musllinux Wheel。

## 致谢

感谢 [`2833844911/cyCronet`](https://github.com/2833844911/cyCronet) 项目及其作者为本项目提供的基座，本项目在其基础上二次开发。

## 版权与免责声明

以下版权声明和许可条款必须完整保留：

```text
MIT License

Copyright (c) 2026 Cronet-Cloak

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

完整许可内容同时保存在 [`LICENSE`](LICENSE) 文件中。
