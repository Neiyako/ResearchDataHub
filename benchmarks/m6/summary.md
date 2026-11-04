- 运行时间：2026-11-04T11:27:04.774986+00:00
- 环境：macOS-27.0.1-arm64-arm-64bit-Mach-O / Python 3.13.13 / arm64

| 条件 | 成功 | 端到端耗时 (s) | 字节数 | 有效吞吐 (MB/s) | 主要阶段 (s) |
|---|---|---|---|---|---|
| ncar | PASS | 113.61 | 1134576716 | 9.99 | catalog=2.63, head=1.43, download=108.73 |
| mixed | PASS | 2702.71 | 1134576716 | 0.42 | cds=2700.0, ncar=112.55 |
| cds | PASS | 30.2 | 25116 | 0.0 | queue_and_generate=28.71, download=1.49 |

混合制先就绪：112.55 s（winner=ncar，冗余字节=0）
