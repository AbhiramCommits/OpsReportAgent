# Scorecard — run `run-001`

Documents scored: **60**  
Overall macro accuracy: **0.975**  
Category accuracy: **0.850**  
Human corrections applied: **23**

## Per-field exact-match accuracy

| field | n | exact | accuracy |
| --- | ---: | ---: | ---: |
| category | 60 | 51 | 0.850 |
| store_id | 60 | 60 | 1.000 |
| incident_date | 60 | 60 | 1.000 |
| vendor | 60 | 60 | 1.000 |
| dollar_impact | 60 | 60 | 1.000 |
| severity | 60 | 60 | 1.000 |
| **macro** | 60 | | **0.975** |

## needs_review flag quality

| metric | value |
| --- | ---: |
| flagged | 10 |
| true positive | 0 |
| false positive | 10 |
| false negative | 9 |
| precision | 0.000 |
| recall | 0.000 |

## Category confusion matrix

```
gold \ pred         Equipment Failure         Facilities        Food Safety   Inventory Shrink      POS/IT Outage    Safety Incident    Vendor Delivery
-------------------------------------------------------------------------------------------------------------------------------------------------------
Equipment Failure                   9                  0                  0                  0                  0                  0                  0
Facilities                          1                  7                  0                  0                  0                  0                  0
Food Safety                         1                  0                  8                  0                  0                  0                  0
Inventory Shrink                    1                  0                  0                  7                  0                  0                  0
POS/IT Outage                       2                  0                  0                  0                  6                  0                  0
Safety Incident                     2                  0                  0                  0                  0                  7                  0
Vendor Delivery                     2                  0                  0                  0                  0                  0                  7
```
