# 0.2.12 打 tag 前的验收记录

按 AGENTS.md 第 13 节生成。`review-before-tag.py` 一次跑完全部步骤会超出本机的后台进程
生存期（长驻进程会被宿主回收），所以拆成两次调用，合并起来覆盖同一组步骤：

| 文件 | 覆盖的组 | 步骤数 | failures |
| --- | --- | --- | --- |
| `gates.json` | `gates` / `bindings` / `fingerprint`（静态部分） | 24 | 0 |
| `runtime.json` | `fingerprint`（线上）/ `concurrency` / `leaks` | 8 | 0 |
| `bench.json` | `concurrency` 原始数字 | — | — |
| `leak.json` | 六种请求形态的 fd / RSS | 6 | 0 |
| `leak-hunt.json` | 13 种形态，每形态 2 次重复（`--requests 20 --batches 8`） | 13 | 0 |

两次调用的退出码都是 0。`rust-format` / `rust-check` / `rust-clippy` / `rust-tests` 记为
`unavailable`（本机 PATH 上没有 cargo），不记为 pass。

重跑命令：

```bash
export LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
       CHROMIUM_SRC=$CHROMIUM_SRC
python3 tools/review-before-tag.py --only bench leak-probe leak-hunt wire-smoke \
  frozen-headers discard-fingerprint session-parity \
  --out docs/review-0.2.12/runtime.json
```

## 这次 tag 与 0.2.11 的差别

- 两个 P0 修复：`discard_cookies` 的缓存模式 fail-closed、`discard_cookies` 的指纹漂移
  被具名门禁钉住（`audit-discard-fingerprint.py`）。
- 证书记忆化改为按 Core **完成顺序**记录，并补 7 条计数型回归（原来 7 条功能测试在
  记忆化被短路后仍然全绿）。
- `AsyncSession` 的位置参数兼容性修复（新参数追加到 `max_clients` 之后）。
- 三个新门禁 + 一个变异脚本：`audit-session-parity.py`、`audit-frozen-headers.py`、
  `audit-discard-fingerprint.py`、`mutation-fingerprint-gates.sh`。
- 泄漏判据的晚期窗口缺陷（真实泄漏被判 flat）修复，见 `docs/LESSONS.md`。
- 九个平台的 Core 全部用 0.2.12 重新构建，产物版本串逐个核对。
