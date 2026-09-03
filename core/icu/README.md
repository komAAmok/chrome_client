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
| **仅 IDNA（当前使用）** | **191,056** | 见 `filter.json` |

字符集转换器占了第二种方案的 97%（6.09 MB）。Core 把响应头以原始字节交给绑定层，
由绑定层自行解码，从不调用 ICU 转换器；而内嵌 6.28 MB 会让每个平台的库从 9.0 MB
涨到 15.3 MB，与轻量化目标相反。所以只保留 IDNA。

这不构成功能回退：在此之前 ICU 完全没有初始化，没有任何现有功能依赖 ICU 转换器。

最终数据集只有 9 个条目：`uts46.nrm`、`nfkc.nrm`、`cnvalias.icu`、`uemoji.icu`、
`ulayout.icu`、`icustd.res`、`icuver.res`、`curr/supplementalData.res`、
`zone/tzdbNames.res`。

## 重新生成

需要 `third_party/icu` 完整源码树（含 102 MB 的 `source/data`）。为避免污染 pin 住
的 Chromium 树，在副本里构建：

```sh
cp -a /home/sj/chromium/src/third_party/icu /tmp/icu-work/icu
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
