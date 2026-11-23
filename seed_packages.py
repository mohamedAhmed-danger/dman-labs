"""Seed 25 Daman Lab packages (24 Basic + WeightCare Follow-up) and city prices.

Requires: pip install openpyxl
Example: python seed_packages.py --excel "/app/data/Test Packages Master1.xlsx" --lab-id 1
Add --apply to commit (default is dry run).
"""
import argparse
import importlib
import re
import sys
import unicodedata
from collections import defaultdict

from openpyxl import load_workbook
from flask import Flask

# Explicit mapping supplied by the project owner. Keys are normalized Excel city headers.
CITY_IDS = {
    "العريش": 6,
    "الاسماعيليه": 7,
    "م الجديده": 4,  # Excel abbreviation for مصر الجديدة
    "مصر الجديده": 4,
    "ارض اللواء": 5,
}


def normalize(value):
    s = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    s = re.sub(r"[\u064b-\u065f\u0670]", "", s)
    s = s.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"}))
    return re.sub(r"\s+", " ", s)


def load_app(module_name):
    mod = importlib.import_module(module_name)
    for name in ("app", "application"):
        app = getattr(mod, name, None)
        if isinstance(app, Flask):
            return app
    factory = getattr(mod, "create_app", None)
    if callable(factory):
        app = factory()
        if isinstance(app, Flask):
            return app
    raise RuntimeError(f"No Flask app instance or zero-argument create_app() found in {module_name}")


def safe_name(package, level):
    # Each package name is unique; do not create Basic/Plus/Premium variants.
    name = package
    if len(name) > 50:
        raise ValueError(f"Package name exceeds Bundle.name 50-character limit: {name!r}")
    return name


def selected_level(package, level):
    # The 25th package has no Basic tier; its only priced tier is Follow-up.
    if package == "Daman WeightCare / Mounjaro":
        return level == "Follow-up"
    return level == "Basic"


def read_data(path):
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        matrix = wb["Package Matrix"]
        test_sheet = wb["Package Test List"]
        test_rows = list(test_sheet.values)
        tests = defaultdict(list)
        categories = {}
        for row in test_rows[1:]:
            if row[0] and row[3] and row[4]:
                tests[(str(row[0]).strip(), str(row[3]).strip())].append(str(row[4]).strip())
                categories[str(row[0]).strip()] = str(row[2] or "").strip()
        matrix_rows = list(matrix.values)
        headers = matrix_rows[0]
        city_headers = [(i, str(headers[i]).strip()) for i in range(5, len(headers)) if headers[i]]
        variants = []
        seen = set()
        for excel_row, row in enumerate(matrix_rows[1:], start=2):
            if not row[0] or not row[2]:
                continue
            package, arabic_name, level = str(row[0]).strip(), str(row[1] or "").strip(), str(row[2]).strip()
            if not selected_level(package, level):
                continue
            name = safe_name(package, level)
            if name in seen:
                raise ValueError(f"Duplicate package name at Excel row {excel_row}: {name}")
            seen.add(name)
            included = tests.get((package, level), [])
            if not included:
                raise ValueError(f"No tests found for {package} / {level} at row {excel_row}")
            expected = row[3]
            if expected is not None and int(expected) != len(included):
                raise ValueError(f"Test count mismatch for {name}: matrix={expected}, detailed={len(included)}")
            # Keep the full level string, including clinical qualifier, in the description.
            description = f"{arabic_name}\nCategory: {categories.get(package, '')}\nLevel: {level}\nTests included ({len(included)}):\n" + "\n".join(included)
            prices = {}
            for idx, header in city_headers:
                value = row[idx] if idx < len(row) else None
                if value is None or str(value).strip() == "":
                    continue
                try:
                    price = float(value)
                except (ValueError, TypeError):
                    raise ValueError(f"Invalid price at row {excel_row}, city {header}: {value!r}")
                if price < 0:
                    raise ValueError(f"Negative price for {name} in {header}")
                prices[header] = price
            variants.append((name, description, prices))
        if len(variants) != 25:
            raise ValueError(f"Expected 25 packages (24 Basic + 1 WeightCare Follow-up), found {len(variants)}")
        return variants, [header for _, header in city_headers]
    finally:
        wb.close()


def resolve_cities(headers, city_models):
    """Use explicit city IDs, verifying each one belongs to the selected lab."""
    cities_by_id = {c.id: c for c in city_models}
    result = {}
    for header in headers:
        city_id = CITY_IDS.get(normalize(header))
        if city_id is None:
            raise ValueError(f"No explicit city ID mapping for Excel header {header!r}; update CITY_IDS")
        city = cities_by_id.get(city_id)
        if city is None:
            raise ValueError(f"City ID {city_id} (Excel column {header!r}) not found in selected laboratory")
        result[header] = city
    if len({c.id for c in result.values()}) != len(headers):
        raise ValueError("Two spreadsheet city columns resolved to the same database city")
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--excel", required=True)
    p.add_argument("--lab-id", type=int, required=True)
    p.add_argument("--app-module", default="app", help="Importable Flask application module")
    p.add_argument("--models-module", default="models.models", help="Importable SQLAlchemy models module")
    p.add_argument("--apply", action="store_true", help="Commit to database (default: dry run)")
    a = p.parse_args()
    variants, headers = read_data(a.excel)
    app = load_app(a.app_module)
    models = importlib.import_module(a.models_module)
    db = app.extensions["sqlalchemy"]
    Bundle = models.Bundle
    BundleCityPrice = models.BundleCityPrice
    City = models.City
    Laboratory = models.Laboratory
    with app.app_context():
        if db.session.get(Laboratory, a.lab_id) is None:
            raise ValueError(f"Laboratory ID {a.lab_id} does not exist")
        cities = City.query.filter_by(laboratory_id=a.lab_id).all()
        mapping = resolve_cities(headers, cities)
        print("City mapping:", {h: f"{c.name} (ID {c.id})" for h, c in mapping.items()})
        print(f"Excel variants: {len(variants)}; nonempty city prices: {sum(len(v[2]) for v in variants)}")
        counters = defaultdict(int)
        try:
            for name, description, prices in variants:
                bundle = Bundle.query.filter_by(name=name).one_or_none()
                if bundle is None:
                    bundle = Bundle(name=name, price=None, description=description)
                    db.session.add(bundle)
                    db.session.flush()
                    counters["new_bundles"] += 1
                else:
                    # Global Bundle names are not scoped to labs. Do NOT change records that might belong to another lab.
                    # Only update existing bundle metadata if the description is already from this spreadsheet.
                    if bundle.description != description:
                        print(f"NOTICE: Existing global bundle {name!r} retained unchanged; verify tenancy/description manually")
                    counters["existing_bundles"] += 1
                for header, price in prices.items():
                    city = mapping[header]
                    cp = BundleCityPrice.query.filter_by(bundle_id=bundle.id, city_id=city.id).one_or_none()
                    if cp is None:
                        db.session.add(BundleCityPrice(bundle_id=bundle.id, city_id=city.id, price=price))
                        counters["new_city_prices"] += 1
                    elif cp.price != price:
                        # Existing prices are preserved to avoid silently overwriting production changes.
                        counters["different_existing_prices"] += 1
                        print(f"NOTICE: Price differs for {name} / {city.name}: DB={cp.price}, Excel={price}; left unchanged")
                    else:
                        counters["unchanged_city_prices"] += 1
            print("Summary:", dict(counters))
            if a.apply:
                db.session.commit()
                print("COMMITTED")
            else:
                db.session.rollback()
                print("DRY RUN: no database changes committed; use --apply when verified")
        except Exception:
            db.session.rollback()
            raise

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
