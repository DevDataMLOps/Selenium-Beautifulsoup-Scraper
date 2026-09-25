"""Combine retailer exports into one clean analysis dataset.

Run after the scrapers:
    python unify.py
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PROJECT_DIR / "extracted_data"
DEFAULT_INPUTS = (
    DEFAULT_DATA_DIR / "eyebuydirect_data.csv",
    DEFAULT_DATA_DIR / "framesdirect_data.csv",
)
DEFAULT_OUTPUT = DEFAULT_DATA_DIR / "unified_eyewear.csv"
REQUIRED_SOURCES = {"eyebuydirect", "framesdirect"}

REQUIRED_COLUMNS = [
    "source",
    "brand",
    "name",
    "former_price",
    "current_price",
    "discount",
    "product_link",
    "page",
]
OUTPUT_COLUMNS = REQUIRED_COLUMNS + [
    "discount_pct",
    "savings",
    "on_sale",
    "sold_by_n_sites",
    "scraped_at",
    "collection_method",
]


def normalized_key(value: object) -> str:
    """Normalize retailer text for cross-site brand/model matching."""
    if pd.isna(value):
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def read_export(path: Path) -> pd.DataFrame:
    """Read and validate one scraper CSV."""
    frame = pd.read_csv(path, encoding="utf-8-sig")
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"{path.name} is missing columns: {', '.join(missing)}")
    return frame[REQUIRED_COLUMNS].copy()


def build_unified_dataset(input_paths: list[Path]) -> tuple[pd.DataFrame, list[str]]:
    """Load, clean, enrich, and deduplicate all available retailer exports."""
    frames: list[pd.DataFrame] = []
    notes: list[str] = []

    for path in input_paths:
        if not path.exists():
            notes.append(f"skipped missing input: {path}")
            continue
        frame = read_export(path)
        if frame.empty:
            notes.append(f"skipped empty input: {path.name}")
            continue
        metadata_path = path.with_name(
            f"{path.stem.removesuffix('_data')}_snapshot_metadata.json"
        )
        metadata: dict = {}
        if (
            metadata_path.exists()
            and metadata_path.stat().st_mtime >= path.stat().st_mtime
        ):
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        frame["_scraped_at"] = metadata.get(
            "collected_at",
            datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        )
        frame["_collection_method"] = metadata.get(
            "collection_method",
            "Selenium Grid",
        )
        frames.append(frame)
        notes.append(f"loaded {len(frame)} rows from {path.name}")

    if not frames:
        return pd.DataFrame(columns=OUTPUT_COLUMNS), notes

    data = pd.concat(frames, ignore_index=True)

    for column in ("source", "brand", "name", "discount", "product_link"):
        data[column] = data[column].astype("string").str.strip()
        data[column] = data[column].replace("", pd.NA)

    for column in ("former_price", "current_price", "page"):
        data[column] = pd.to_numeric(data[column], errors="coerce")

    before_required = len(data)
    data = data.dropna(subset=["source", "name", "current_price", "product_link"])
    data = data[data["current_price"] >= 0].copy()
    dropped_required = before_required - len(data)
    if dropped_required:
        notes.append(f"dropped {dropped_required} rows with invalid required values")

    duplicate_count = int(data.duplicated(["source", "product_link"]).sum())
    data = data.drop_duplicates(["source", "product_link"], keep="first").copy()
    if duplicate_count:
        notes.append(f"removed {duplicate_count} duplicate product links")

    valid_former = (
        data["former_price"].notna()
        & (data["former_price"] > data["current_price"])
    )
    data.loc[~valid_former, "former_price"] = pd.NA
    data["on_sale"] = valid_former
    data["savings"] = (data["former_price"] - data["current_price"]).round(2)
    data["discount_pct"] = (
        data["savings"].div(data["former_price"]).mul(100).round(2)
    )

    calculated_discount = data["discount_pct"].round().astype("Int64").astype("string") + "%"
    data.loc[data["on_sale"], "discount"] = calculated_discount[data["on_sale"]]
    data.loc[~data["on_sale"], "discount"] = pd.NA

    match_key = (
        data["brand"].map(normalized_key)
        + "|"
        + data["name"].map(normalized_key)
    )
    site_counts = data.groupby(match_key, dropna=False)["source"].transform("nunique")
    data["sold_by_n_sites"] = site_counts.astype(int)
    data["scraped_at"] = data.pop("_scraped_at")
    data["collection_method"] = data.pop("_collection_method")
    data["page"] = data["page"].astype("Int64")

    data = data.sort_values(
        ["source", "brand", "name"],
        na_position="last",
        kind="stable",
    ).reset_index(drop=True)
    return data[OUTPUT_COLUMNS], notes


def write_outputs(data: pd.DataFrame, csv_path: Path) -> tuple[Path, Path]:
    """Write the unified dataset as CSV and JSON."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path = csv_path.with_suffix(".json")
    data.to_csv(csv_path, index=False, encoding="utf-8-sig")

    records = json.loads(data.to_json(orient="records", date_format="iso"))
    json_path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return csv_path, json_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        default=list(DEFAULT_INPUTS),
        help="retailer CSV files (defaults to both scraper outputs)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"unified CSV destination (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="return success when only one retailer has usable rows",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        data, notes = build_unified_dataset(args.inputs)
        csv_path, json_path = write_outputs(data, args.output)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        print(f"Unifier failed ({type(exc).__name__}): {exc}")
        return 1

    for note in notes:
        print(f"- {note}")
    print(f"Saved {len(data)} unified rows to:")
    print(f"  {csv_path}")
    print(f"  {json_path}")
    if data.empty:
        print("No usable scraper rows were available.")
        return 1
    present_sources = set(data["source"].dropna().astype(str))
    missing_sources = sorted(REQUIRED_SOURCES - present_sources)
    if missing_sources and not args.allow_partial:
        print(
            "Incomplete mandatory source coverage: "
            + ", ".join(missing_sources)
            + ". Use --allow-partial only for local dashboard testing."
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
