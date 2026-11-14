# docs/perf 索引

| 文件 | 内容 | 生成方式 |
| --- | --- | --- |
| `matrix.json` | 并发梯度 × 请求形态 × 故障注入矩阵 | `tools/load-matrix.py` |
| `thread-ladder.json` | 同步线程阶梯与两个拐点 | `tools/thread-ladder.py` |
| `leak-before-fix.json` / `leak-after-fix.json` | 泄漏缺陷修复前后（含 `churn-nogc`） | `tools/leak-hunt.py` |
| `leak-after-fix-churn.json` | 上述的 `churn-nogc` 单形态明细 | `tools/leak-hunt.py` |
| `repro_churn_nogc.py` | 该形态的最小复现（自带服务器） | 由 `leak-hunt.py` 生成 |
| `ab-gate-0.2.11/` | 两处 P0 修复的并发 A/B，含换序复测 | `tools/concurrency-ab.py` + `tools/mtls-concurrency.py` + `tools/perf-ab-summary.py` |

每条记录都可以用对应工具重跑；`ab-gate-0.2.11/summary*.json` 由原始 JSON 重算，
不手抄数字。
