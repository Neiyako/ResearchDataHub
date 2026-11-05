# 全链路测速基准 — earthdata

- 生成时间：2026-11-05T17:18:25.341516+00:00
- 环境：macOS-27.0.1-arm64-arm-64bit-Mach-O / Python 3.13.13
- 参数：samples=8，stop_response_rate=1.0，sample_timeout=180.0s

| 路径 | 尝试 | 成功 | 失败 | 超时 | 未测 | 响应率 | 中位耗时(s) | P95(s) | 中位吞吐(MB/s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 8 | 8 | 0 | 0 | 0 | 100% | 3.502 | 7.618 | 0.05 |
| rdh | 8 | 8 | 0 | 0 | 0 | 100% | 6.035 | 6.77 | 0.03 |

## 逐样本

| 路径 | # | 状态 | 字节 | 耗时(s) | 吞吐(MB/s) | 错误 |
|---|---:|---|---:|---:|---:|---|
| baseline | 0 | SUCCESS | 176559 | 9.018 | 0.02 |  |
| baseline | 1 | SUCCESS | 176559 | 3.506 | 0.05 |  |
| baseline | 2 | SUCCESS | 176559 | 3.984 | 0.04 |  |
| baseline | 3 | SUCCESS | 176559 | 3.297 | 0.05 |  |
| baseline | 4 | SUCCESS | 176559 | 3.497 | 0.05 |  |
| baseline | 5 | SUCCESS | 176559 | 3.254 | 0.05 |  |
| baseline | 6 | SUCCESS | 176559 | 5.018 | 0.03 |  |
| baseline | 7 | SUCCESS | 176559 | 3.248 | 0.05 |  |
| rdh | 0 | SUCCESS | 176559 | 6.861 | 0.02 |  |
| rdh | 1 | SUCCESS | 176559 | 5.407 | 0.03 |  |
| rdh | 2 | SUCCESS | 176559 | 6.602 | 0.03 |  |
| rdh | 3 | SUCCESS | 176559 | 5.558 | 0.03 |  |
| rdh | 4 | SUCCESS | 176559 | 6.531 | 0.03 |  |
| rdh | 5 | SUCCESS | 176559 | 6.127 | 0.03 |  |
| rdh | 6 | SUCCESS | 176559 | 5.942 | 0.03 |  |
| rdh | 7 | SUCCESS | 176559 | 5.192 | 0.03 |  |

## 说明

- **url**：https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/MOD11A1.061/MOD11A1.A2000055.h12v10.061.2020043121102/MOD11A1.A2000055.h12v10.061.2020043121102.hdf
- **short_name**：MOD11A1
- **declared_size_mb**：0.0
- **auth**：EDL bearer token（值不落盘、不打印）
- **baseline_retries**：1
- **comparison_valid**：True
