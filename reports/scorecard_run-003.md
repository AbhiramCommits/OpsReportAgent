# Scorecard — run `run-003`

Documents scored: **60**  
Overall macro accuracy: **0.989**  
Category accuracy: **0.933**  
Human corrections applied: **0**

## Per-field exact-match accuracy

| field | n | exact | accuracy |
| --- | ---: | ---: | ---: |
| category | 60 | 56 | 0.933 |
| store_id | 60 | 60 | 1.000 |
| incident_date | 60 | 60 | 1.000 |
| vendor | 60 | 60 | 1.000 |
| dollar_impact | 60 | 60 | 1.000 |
| severity | 60 | 60 | 1.000 |
| **macro** | 60 | | **0.989** |

## needs_review flag quality

| metric | value |
| --- | ---: |
| flagged | 5 |
| true positive | 4 |
| false positive | 1 |
| false negative | 0 |
| precision | 0.800 |
| recall | 1.000 |

## Category confusion matrix

```
gold \ pred         Equipment Failure         Facilities        Food Safety   Inventory Shrink      POS/IT Outage    Safety Incident    Vendor Delivery
-------------------------------------------------------------------------------------------------------------------------------------------------------
Equipment Failure                   9                  0                  0                  0                  0                  0                  0
Facilities                          1                  7                  0                  0                  0                  0                  0
Food Safety                         1                  0                  8                  0                  0                  0                  0
Inventory Shrink                    0                  0                  0                  8                  0                  0                  0
POS/IT Outage                       0                  0                  0                  0                  8                  0                  0
Safety Incident                     1                  0                  0                  0                  0                  8                  0
Vendor Delivery                     1                  0                  0                  0                  0                  0                  8
```
