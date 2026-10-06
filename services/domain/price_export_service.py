"""
price_export_service.py

Exports the LabService catalog for a specific laboratory + city to a styled
Excel workbook, using the shared excel_utils formatter.

Columns exported (RTL order, Arabic headers):
    اسم التحليل | السعر | نوع العينة | المدة (ساعة) | الوصف

Returns:
    BytesIO containing the .xlsx file ready to send as a Flask response.
"""
import logging
from io import BytesIO

import openpyxl

from models.models import db, LabService, City, Laboratory
from utils.excel_utils import format_excel_sheet

logger = logging.getLogger(__name__)

# These names MUST match the import column aliases exactly so that an
# exported file can be edited and re-imported without renaming headers.
_HEADERS = [
    "Test Name",      # maps to _NAME_ALIASES in price_import_service
    "Price",          # maps to _PRICE_ALIASES
    "Test Details",   # maps to _DESC_ALIASES
    "Sample Type",    # maps to _SAMPLE_ALIASES
    "Duration",       # maps to _DURATION_ALIASES
]


def _build_rows(services: list[LabService]) -> list[list]:
    rows = []
    for s in services:
        rows.append([
            s.name or "",
            float(s.price) if s.price is not None else "",
            s.description or "",
            s.sample_type or "",
            s.duration or "",
        ])
    return rows


def export_city_price_list(laboratory_id: int, city_id: int) -> tuple[BytesIO, str]:
    """
    Build and return an Excel workbook for the given city's catalog.

    Args:
        laboratory_id: Laboratory that owns the catalog.
        city_id:       City to export.

    Returns:
        (BytesIO, filename)  — BytesIO contains the .xlsx bytes.

    Raises:
        ValueError: if city not found or belongs to a different laboratory.
    """
    city = db.session.get(City, city_id)
    if not city:
        raise ValueError("المدينة غير موجودة")
    if city.laboratory_id != laboratory_id:
        raise ValueError("المدينة لا تنتمي إلى هذا المعمل")

    lab = db.session.get(Laboratory, laboratory_id)
    lab_name = lab.name if lab else str(laboratory_id)

    services = (
        LabService.query
        .filter_by(laboratory_id=laboratory_id, city_id=city_id)
        .order_by(LabService.name.asc())
        .all()
    )

    wb = openpyxl.Workbook()
    ws = wb.active

    rows = _build_rows(services)
    sheet_title = f"{city.name}"[:31]  # Excel sheet name max 31 chars

    format_excel_sheet(
        ws,
        title=sheet_title,
        headers=_HEADERS,
        rows=rows,
        status_col_idx=None,
    )

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_city = city.name.replace(" ", "_").replace("/", "-")
    safe_lab = lab_name.replace(" ", "_").replace("/", "-")
    filename = f"price_list_{safe_lab}_{safe_city}.xlsx"

    return buf, filename


def export_all_cities_price_list(laboratory_id: int) -> tuple[BytesIO, str]:
    """
    Build an Excel workbook with one sheet per city for the given laboratory.

    Returns:
        (BytesIO, filename)
    """
    from services.domain.city_service import CityService

    cities = CityService.get_cities_by_laboratory(laboratory_id)
    if not cities:
        raise ValueError("لا توجد مناطق مسجّلة لهذا المعمل")

    lab = db.session.get(Laboratory, laboratory_id)
    lab_name = lab.name if lab else str(laboratory_id)

    wb = openpyxl.Workbook()
    # Remove default empty sheet
    wb.remove(wb.active)

    for city in cities:
        services = (
            LabService.query
            .filter_by(laboratory_id=laboratory_id, city_id=city.id)
            .order_by(LabService.name.asc())
            .all()
        )
        ws = wb.create_sheet(title=city.name[:31])
        rows = _build_rows(services)
        format_excel_sheet(
            ws,
            title=city.name[:31],
            headers=_HEADERS,
            rows=rows,
            status_col_idx=None,
        )

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_lab = lab_name.replace(" ", "_").replace("/", "-")
    filename = f"price_list_{safe_lab}_all_cities.xlsx"

    return buf, filename
