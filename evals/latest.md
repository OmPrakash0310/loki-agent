# Loki eval 20261017-191027 (claude-haiku-4-5)

| Scenario | Runs | Correct | Accuracy | Avg time | Avg cost | Avg steps |
|---|---|---|---|---|---|---|
| bad_deploy | 5 | 5 | 100% | 99s | $0.0181 | 3.2 |
| slow_db | 5 | 4 | 80% | 122s | $0.0300 | 6.8 |
| timeout | 5 | 5 | 100% | 134s | $0.0297 | 7.2 |
| **Total** | 15 | 14 | **93%** | | $0.3893 total | |

## Misses
- slow_db run 5 (#41): missed right_cause_type. Loki said: payments-svc experienced sudden latency spike (0.23s to 5.75s p95) starting at 19:39:40, causing shop-api timeouts, while its external dependencies remained res
