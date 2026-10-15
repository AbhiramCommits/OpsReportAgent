# Scorecard — run `run-001`

Documents scored: **60**  
Overall macro accuracy: **0.911**  
Category accuracy: **0.717**  
Human corrections applied: **0**

## Per-field exact-match accuracy

| field | n | exact | accuracy |
| --- | ---: | ---: | ---: |
| category | 60 | 43 | 0.717 |
| store_id | 60 | 60 | 1.000 |
| incident_date | 60 | 50 | 0.833 |
| vendor | 60 | 60 | 1.000 |
| dollar_impact | 60 | 55 | 0.917 |
| severity | 60 | 60 | 1.000 |
| **macro** | 60 | | **0.911** |

## needs_review flag quality

| metric | value |
| --- | ---: |
| flagged | 10 |
| true positive | 10 |
| false positive | 0 |
| false negative | 9 |
| precision | 1.000 |
| recall | 0.526 |

## Category confusion matrix

```
gold \ pred         Equipment Failure         Facilities        Food Safety   Inventory Shrink      POS/IT Outage    Safety Incident    Vendor Delivery
-------------------------------------------------------------------------------------------------------------------------------------------------------
Equipment Failure                   9                  0                  0                  0                  0                  0                  0
Facilities                          2                  6                  0                  0                  0                  0                  0
Food Safety                         3                  0                  6                  0                  0                  0                  0
Inventory Shrink                    2                  0                  0                  6                  0                  0                  0
POS/IT Outage                       2                  0                  0                  0                  6                  0                  0
Safety Incident                     4                  0                  0                  0                  0                  5                  0
Vendor Delivery                     4                  0                  0                  0                  0                  0                  5
```
