"""Shared ops taxonomy."""

from __future__ import annotations

CATEGORIES: tuple[str, ...] = (
    "Equipment Failure",
    "Food Safety",
    "Vendor Delivery",
    "Safety Incident",
    "POS/IT Outage",
    "Facilities",
    "Inventory Shrink",
)

SEVERITIES: tuple[str, ...] = ("Low", "Medium", "High", "Critical")

TAXONOMY = "\n".join(f"- {category}" for category in CATEGORIES)
