# 全链路测速基准 — ncar

- 生成时间：2026-11-05T17:33:48.333475+00:00
- 环境：macOS-27.0.1-arm64-arm-64bit-Mach-O / Python 3.13.13
- 参数：samples=8，stop_response_rate=1.0，sample_timeout=300.0s

| 路径 | 尝试 | 成功 | 失败 | 超时 | 未测 | 响应率 | 中位耗时(s) | P95(s) | 中位吞吐(MB/s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 8 | 7 | 1 | 0 | 0 | 88% | 15.751 | 20.337 | 3.53 |
| rdh | 8 | 8 | 0 | 0 | 0 | 100% | 14.178 | 19.603 | 3.92 |

## 逐样本

| 路径 | # | 状态 | 字节 | 耗时(s) | 吞吐(MB/s) | 错误 |
|---|---:|---|---:|---:|---:|---|
| baseline | 0 | SUCCESS | 58285971 | 20.691 | 2.69 |  |
| baseline | 1 | SUCCESS | 58285971 | 15.562 | 3.57 |  |
| baseline | 2 | SUCCESS | 58285971 | 15.691 | 3.54 |  |
| baseline | 3 | SUCCESS | 58285971 | 19.511 | 2.85 |  |
| baseline | 4 | FAILED | 0 | 125.954 | 0.00 | RemoteProtocolError: Server disconnected without sending a response. |
| baseline | 5 | SUCCESS | 58285971 | 14.522 | 3.83 |  |
| baseline | 6 | SUCCESS | 58285971 | 15.751 | 3.53 |  |
| baseline | 7 | SUCCESS | 58285971 | 16.288 | 3.41 |  |
| rdh | 0 | SUCCESS | 58285971 | 14.603 | 3.81 |  |
| rdh | 1 | SUCCESS | 58285971 | 13.752 | 4.04 |  |
| rdh | 2 | SUCCESS | 58285971 | 20.056 | 2.77 |  |
| rdh | 3 | SUCCESS | 58285971 | 17.046 | 3.26 |  |
| rdh | 4 | SUCCESS | 58285971 | 12.316 | 4.51 |  |
| rdh | 5 | SUCCESS | 58285971 | 18.762 | 2.96 |  |
| rdh | 6 | SUCCESS | 58285971 | 10.359 | 5.37 |  |
| rdh | 7 | SUCCESS | 58285971 | 11.433 | 4.86 |  |

## 说明

- **url**：https://data.gdex.ucar.edu/d633000/e5.oper.an.sfc/202407/e5.oper.an.sfc.128_032_asn.ll025sc.2024070100_2024073123.nc
- **baseline_retries**：1
- **comparison_valid**：True
- **note**：rdh 路径走完整 ResearchDataHub 链路（计划→调度→传输→校验→提交→交付）
