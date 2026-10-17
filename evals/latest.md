# Loki eval 20261017-184945 (fake)

| Scenario | Runs | Correct | Accuracy | Avg time | Avg cost | Avg steps |
|---|---|---|---|---|---|---|
| bad_deploy | 1 | 1 | 100% | 81s | $0.0000 | 3.0 |
| slow_db | 1 | 0 | 0% | 86s | $0.0000 | 2.0 |
| timeout | 1 | 0 | 0% | 91s | $0.0000 | 2.0 |
| **Total** | 3 | 1 | **33%** | | $0.0000 total | |

## Misses
- slow_db run 1 (#29): missed right_service, right_cause_type, names_database. Loki said: [fake model] payments-svc looks like the source
- timeout run 1 (#30): missed right_service, right_cause_type, names_card_network. Loki said: [fake model] payments-svc looks like the source
