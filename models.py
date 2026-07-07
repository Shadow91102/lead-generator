"""Shared data model + CSV writer for leads."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field, asdict


@dataclass
class Business:
    name: str = ""
    category: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""
    emails: list[str] = field(default_factory=list)
    instagram: str = ""
    facebook: str = ""
    linkedin: str = ""
    tiktok: str = ""
    youtube: str = ""
    rating: str = ""
    reviews: str = ""
    source: str = ""


# Column order for the CSV output.
CSV_FIELDS = [
    "name",
    "category",
    "address",
    "phone",
    "website",
    "emails",
    "instagram",
    "facebook",
    "linkedin",
    "tiktok",
    "youtube",
    "rating",
    "reviews",
    "source",
]


def write_csv(businesses: list[Business], path: str) -> None:
    """Write leads to a UTF-8-BOM CSV (BOM so Excel shows accents correctly)."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for b in businesses:
            row = asdict(b)
            row["emails"] = "; ".join(b.emails)
            writer.writerow(row)
