# ICU 数据

`icudtl.dat` 是编入 `libminicronet` 的 ICU 数据集，只保留 Core 真正会用到的部分。
`filter.json` 是生成它的 ICU 数据过滤器。

## 为什么需要它

Core 从 `net`/`url` 间接链接了 ICU，Chromium 的 URL 规范化需要 UTS #46 来处理
国际化主机名。在这份数据集加入之前，Core 链接了 ICU 但从不调用
`base::i18n::InitializeICU()`，于是任何非 ASCII 主机名都会让规范化 CHECK 失败，
以 SIGTRAP 打掉宿主进程：

```python
chrome_client.get("http://例え.テスト/")   # 曾经终止整个进程
```

现在 `engine.cc` 在 `Runtime` 构造时初始化 ICU，失败则 Engine 创建返回
`MN_ERROR_INITIALIZATION_FAILED`，而不是留到规范化时崩溃。

## 体积取舍

同一份 ICU 源码用三种过滤器构建的结果：

| 数据集 | 大小 | 说明 |
| --- | --- | --- |
| Chromium `common/icudtl.dat` | 10,876,560 | 完整数据，此前仅 Windows 以外挂文件携带 |
| IDNA + 全部字符集转换器 | 6,276,640 | 见 `filter-idna-plus-uconv.json` |
| 仅 IDNA（已废弃） | 191,056 | 排除了 `conversion_mappings`，见下文「代理认证事故」 |
| **IDNA + ISO-8859-1（当前使用）** | **194,064** | 见 `filter.json` |

字符集转换器占了第二种方案的 97%（6.09 MB）。Core 把响应头以原始字节交给绑定层，
由绑定层自行解码，所以那 6 MB 里除了一张单字节表之外都没有消费者；内嵌 6.28 MB 会让
每个平台的库从 9.0 MB 涨到 15.3 MB，与轻量化目标相反。

## 代理认证事故：`conversion_mappings` 不能整个排除

**曾经写在这里的断言「没有任何现有功能依赖 ICU 转换器」是错的，代价是代理认证全线
失效。** 真正需要转换器的消费者是 HTTP Basic/Digest 的 realm 解码：

```
net/http/http_auth_handler_basic.cc:53   ConvertToUtf8AndNormalize(value, kCharsetLatin1, realm)
base/i18n/icu_string_conversions.cc:202  CodepageToUTF16(text, "ISO-8859-1", FAIL, &utf16)
base/i18n/icu_string_conversions.cc:167  ucnv_open("ISO-8859-1", &status)   // 打不开就 return false
```

而 `ISO-8859-1` 在 ICU 里是 `windows-1252-html` 转换器的别名
（`source/data/mappings/convrtrs.txt:302`）。数据集里没有 `.cnv` 时 `ucnv_open`
失败，`ParseRealm` 返回 false，`HttpAuthHandlerBasic::Factory::CreateAuthHandler`
返回 `ERR_INVALID_RESPONSE`，`HttpAuth::ChooseBestChallenge` 于是丢掉这个 challenge，
`handler_` 保持空 → `HandleAuthChallenge` 不调用 `PopulateAuthChallenge()` →
`GetAuthChallengeInfo()` 返回 nullptr → `URLRequestJob::NotifyHeadersComplete` 从不调用
`NotifyAuthRequired()` → Core 的 `Request::OnAuthRequired` **一次也不会被调用**。

现象是带 realm 的挑战永远认证不了，而且没有任何报错：

```python
chrome_client.Session(proxy=..., proxy_auth=("user", "pass"))
# Proxy-Authenticate: Basic realm="p"   -> 407，凭据从未发出
# Proxy-Authenticate: Basic             -> 200（无 realm 时才正常）
```

所以 `filter.json` 现在用 `includelist` 只保留这一张表：

```json
"conversion_mappings": { "includelist": [ "windows-1252-html" ] }
```

代价 3,008 字节（191,056 → 194,064）。验收见
`bindings/python/tests/test_compat.py` 的 `ProxyAuthenticationTests`。

> `filter.json` 的注释必须**顶格**写：ICU 的 `CommentStripper` 只剥离行首的 `//`，
> 带缩进的注释会让 `configure` 以 `JSONDecodeError` 失败。

最终数据集有 10 个条目：`uts46.nrm`、`nfkc.nrm`、`cnvalias.icu`、`uemoji.icu`、
`ulayout.icu`、`icustd.res`、`icuver.res`、`curr/supplementalData.res`、
`zone/tzdbNames.res`、`windows-1252-html.cnv`。

（`normalization` 里的 `nfc` 是冗余的：`normalizer2impl.h:40` 把
`NORM2_HARDCODE_NFC_DATA` 定为 1，NFC 数据编在代码里而不是数据集里，所以数据集里
只有 `nfkc.nrm` 与 `uts46.nrm`。）

## 重新生成

需要 `third_party/icu` 完整源码树（含 102 MB 的 `source/data`）。为避免污染 pin 住
的 Chromium 树，在副本里构建：

```sh
cp -a "$CHROMIUM_SRC/third_party/icu" /tmp/icu-work/icu
cp core/icu/filter.json /tmp/icu-work/icu/filters/minicronet.json

cd /tmp/icu-work/icu && mkdir -p build && cd build
../source/runConfigureICU Linux/gcc --disable-tests --disable-layoutex \
  --enable-rpath --prefix="$(pwd)"
make -j8                              # 先构建 ICU 工具

(cd data && make clean)
ICU_DATA_FILTER_FILE=/tmp/icu-work/icu/filters/minicronet.json \
  ../source/runConfigureICU Linux/gcc --disable-tests --disable-layoutex \
  --enable-rpath --prefix="$(pwd)"
make -j8                              # 再构建过滤后的数据

cp data/out/tmp/icudt78l.dat <repo>/core/icu/icudtl.dat
```

输出文件名里的 `78` 是 ICU 主版本号，升级 Chromium 后会变。

`tools/sync-core.sh` 负责把 `core/icu/icudtl.dat` 安装到
`third_party/icu/minicronet/`，并应用 `core/patches/icu-minicronet-data.patch`
（让 `is_minicronet_build` 选中这个数据目录）。构建脚本用
`icu_use_data_file = false` 把它编入库中，因此不再需要随产物携带外挂文件。

## 验收

IDN 主机名必须与其 punycode 形式行为一致 —— 都走到 DNS 解析，而不是一个被拒绝、
一个成功。`bindings/python/tests/test_stability.py` 的
`test_internationalized_host_is_canonicalized` 覆盖这一点。
