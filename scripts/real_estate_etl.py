from pathlib import Path
import json
import re

import pandas as pd


NUMERIC_COLUMNS = [
    "residential_units",
    "commercial_units",
    "total_units",
    "land_square_feet",
    "gross_square_feet",
    "year_built",
    "sale_price",
]

STRING_COLUMNS = [
    "neighborhood",
    "building_class_category",
    "tax_class_at_present",
    "easement",
    "building_class_at_present",
    "address",
    "apartment_number",
    "tax_class_at_time_of_sale",
    "building_class_at_time_of_sale",
]


def normalize_column_name(value: str) -> str:
    value = str(value).replace("\n", " ").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")


def clean_and_split(
    input_dir: str | Path,
    output_dir: str | Path,
    drift_start: str = "2024-10-01",
    minimum_sale_price: float = 10_000,
) -> dict:
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    frames = []

    for path in sorted(input_dir.glob("2024_*.xlsx")):
        # The actual column header is on Excel row 7.
        frame = pd.read_excel(path, header=6)
        frame.columns = [normalize_column_name(c) for c in frame.columns]

        frame = frame.dropna(how="all")
        frame["source_file"] = path.name
        frame["source_borough"] = (
            path.stem.removeprefix("2024_")
            .replace("_", " ")
            .title()
        )

        frames.append(frame)

    if not frames:
        raise FileNotFoundError(f"No 2024_*.xlsx files found in {input_dir}")

    data = pd.concat(frames, ignore_index=True)
    raw_row_count = len(data)

    # Normalize text without imputing missing values.
    for column in STRING_COLUMNS:
        if column in data:
            data[column] = (
                data[column]
                .astype("string")
                .str.replace(r"\s+", " ", regex=True)
                .str.strip()
                .replace("", pd.NA)
            )

    # Block, lot, and ZIP are identifiers—not continuous measurements.
    for column in ["block", "lot"]:
        if column in data:
            data[column] = (
                pd.to_numeric(data[column], errors="coerce")
                .astype("Int64")
                .astype("string")
            )

    if "zip_code" in data:
        data["zip_code"] = (
            pd.to_numeric(data["zip_code"], errors="coerce")
            .astype("Int64")
            .astype("string")
            .str.zfill(5)
        )

    # Normalize numeric columns.
    for column in NUMERIC_COLUMNS:
        if column in data:
            data[column] = pd.to_numeric(data[column], errors="coerce")

    data["sale_date"] = pd.to_datetime(data["sale_date"], errors="coerce")

    # Zero square footage generally means unavailable, not a zero-size property.
    for column in ["land_square_feet", "gross_square_feet"]:
        data[column] = data[column].mask(data[column] <= 0)

    # Preserve the row but treat impossible/unknown construction years as missing.
    sale_year = data["sale_date"].dt.year
    invalid_year = (
        (data["year_built"] <= 0)
        | (data["year_built"] > sale_year)
        | (data["year_built"] < 1600)
    )
    data.loc[invalid_year, "year_built"] = pd.NA

    # Record rejected rows instead of silently deleting them.
    rejection_reason = pd.Series("", index=data.index, dtype="string")

    def reject(mask: pd.Series, reason: str) -> None:
        existing = rejection_reason.loc[mask]
        rejection_reason.loc[mask] = existing.apply(
            lambda value: reason if not value else f"{value}|{reason}"
        )

    reject(data["sale_date"].isna(), "invalid_sale_date")
    reject(
        data["sale_date"].notna() & data["sale_date"].dt.year.ne(2024),
        "sale_date_outside_2024",
    )
    reject(data["sale_price"].isna(), "invalid_sale_price")
    reject(
        data["sale_price"].notna()
        & data["sale_price"].lt(minimum_sale_price),
        "nominal_or_non_market_sale",
    )

    # Identify repeated representations of the same transaction.
    dedupe_columns = [
        "source_borough",
        "block",
        "lot",
        "apartment_number",
        "sale_date",
        "sale_price",
    ]

    valid_so_far = rejection_reason.eq("")
    duplicate_rows = data.loc[valid_so_far].duplicated(
        subset=dedupe_columns,
        keep="first",
    )
    duplicate_indexes = duplicate_rows[duplicate_rows].index
    reject(data.index.isin(duplicate_indexes), "duplicate_sale")

    data["rejection_reason"] = rejection_reason

    rejected = data[data["rejection_reason"].ne("")].copy()
    clean = data[data["rejection_reason"].eq("")].copy()
    clean = clean.drop(columns="rejection_reason")

    cutoff = pd.Timestamp(drift_start)

    reference = clean[clean["sale_date"] < cutoff].copy()
    drift_holdout = clean[clean["sale_date"] >= cutoff].copy()

    reference = reference.sort_values("sale_date").reset_index(drop=True)
    drift_holdout = drift_holdout.sort_values("sale_date").reset_index(drop=True)
    rejected = rejected.reset_index(drop=True)

    # Parquet retains dates, numbers, nulls, and strings correctly.
    reference.to_parquet(output_dir / "reference_2024.parquet", index=False)
    drift_holdout.to_parquet(
        output_dir / "drift_holdout_2024_q4.parquet",
        index=False,
    )
    rejected.to_parquet(output_dir / "rejected_rows.parquet", index=False)

    summary = {
        "raw_rows": raw_row_count,
        "clean_rows": len(clean),
        "reference_rows": len(reference),
        "drift_holdout_rows": len(drift_holdout),
        "rejected_rows": len(rejected),
        "drift_start": str(cutoff.date()),
        "minimum_sale_price": minimum_sale_price,
        "rejection_counts": (
            rejected["rejection_reason"]
            .value_counts()
            .to_dict()
        ),
    }

    (output_dir / "quality_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    return summary