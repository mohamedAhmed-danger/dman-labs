"""
tests/test_import_and_catalog.py

Comprehensive test suite covering per-city catalog isolation, Excel price imports,
deterministic parsing rules, deduplication, tenant isolation, and error guards.

Requirements:
- MySQL database (or test DB session)
- Qdrant instance
- Redis instance (for preview tokens & progress tracking)
"""

import io
import pytest
from unittest.mock import patch, MagicMock
import openpyxl

from models.models import db, LabService, Laboratory, City, Branch, User
from services.domain.tests_service import TestsService
from services.domain.city_service import CityService
from services.domain.branch_service import BranchService
from services.domain.price_import_service import (
    preview_import,
    commit_import,
    _cell_float,
    _desc_changed,
    _normalize_name,
)

pytestmark = [pytest.mark.mysql, pytest.mark.qdrant, pytest.mark.redis]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def test_app():
    """Creates a Flask test application context."""
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        yield app


@pytest.fixture
def sample_labs_and_cities(test_app):
    """
    Creates temporary Laboratory and City fixtures for testing,
    and cleans them up after test completion.
    """
    lab_a = Laboratory(name="Test Lab A", location="Cairo", description="Lab A")
    lab_b = Laboratory(name="Test Lab B", location="Alex", description="Lab B")
    db.session.add_all([lab_a, lab_b])
    db.session.flush()

    city_a1 = City(laboratory_id=lab_a.id, name="Cairo-Zone1", sort_order=1)
    city_a2 = City(laboratory_id=lab_a.id, name="Cairo-Zone2", sort_order=2)
    city_b1 = City(laboratory_id=lab_b.id, name="Alex-Main", sort_order=1)
    db.session.add_all([city_a1, city_a2, city_b1])
    db.session.commit()

    yield {
        "lab_a": lab_a,
        "lab_b": lab_b,
        "city_a1": city_a1,
        "city_a2": city_a2,
        "city_b1": city_b1,
    }

    # Clean up DB
    try:
        LabService.query.filter(LabService.laboratory_id.in_([lab_a.id, lab_b.id])).delete()
        Branch.query.filter(Branch.city_id.in_([city_a1.id, city_a2.id, city_b1.id])).delete()
        City.query.filter(City.id.in_([city_a1.id, city_a2.id, city_b1.id])).delete()
        Laboratory.query.filter(Laboratory.id.in_([lab_a.id, lab_b.id])).delete()
        db.session.commit()
    except Exception:
        db.session.rollback()


def _create_excel_bytes(rows: list[list]) -> io.BytesIO:
    """Helper: Creates an in-memory Excel file (.xlsx) from a list of rows."""
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

def test_same_name_in_two_cities_accepted(sample_labs_and_cities):
    """1. Test that the same test name in two different cities of the same lab is ACCEPTED."""
    lab_a = sample_labs_and_cities["lab_a"]
    c1 = sample_labs_and_cities["city_a1"]
    c2 = sample_labs_and_cities["city_a2"]

    ts = TestsService(laboratory_id=lab_a.id)

    with patch("services.domain.tests_service.upsert_test_vector") as mock_upsert:
        t1, msg1 = ts.create_lab_service(
            laboratory_id=lab_a.id, city_id=c1.id, name="CBC Test", price=150.00
        )
        t2, msg2 = ts.create_lab_service(
            laboratory_id=lab_a.id, city_id=c2.id, name="CBC Test", price=180.00
        )

        assert t1 is not None, f"Failed for c1: {msg1}"
        assert t2 is not None, f"Failed for c2: {msg2}"
        assert t1.id != t2.id
        assert t1.city_id == c1.id
        assert t2.city_id == c2.id


def test_same_name_twice_in_one_city_rejected(sample_labs_and_cities):
    """2. Test that duplicate test names within the same city are REJECTED."""
    lab_a = sample_labs_and_cities["lab_a"]
    c1 = sample_labs_and_cities["city_a1"]

    ts = TestsService(laboratory_id=lab_a.id)

    with patch("services.domain.tests_service.upsert_test_vector"):
        t1, msg1 = ts.create_lab_service(
            laboratory_id=lab_a.id, city_id=c1.id, name="Lipid Profile", price=200.00
        )
        assert t1 is not None

        t2, msg2 = ts.create_lab_service(
            laboratory_id=lab_a.id, city_id=c1.id, name="Lipid Profile", price=250.00
        )
        assert t2 is None
        assert "موجود بالفعل في هذه المدينة" in msg2


def test_import_isolation_per_city(sample_labs_and_cities):
    """3. Test that importing an Excel file into City 1 isolates created tests to City 1."""
    lab = sample_labs_and_cities["lab_a"]
    c1 = sample_labs_and_cities["city_a1"]
    c2 = sample_labs_and_cities["city_a2"]

    excel_file = _create_excel_bytes([
        ["Test Name", "Price", "Test Details"],
        ["SGPT / ALT", "120.00", "Liver enzyme test"],
        ["SGOT / AST", "120.00", "Liver enzyme test"],
    ])

    prev = preview_import(
        file_obj=excel_file,
        filename="prices.xlsx",
        laboratory_id=lab.id,
        city_id=c1.id,
    )
    assert prev["error"] is None
    assert prev["new"] == 2

    with patch("services.domain.price_import_service.upsert_test_vectors_batch"):
        comm = commit_import(
            token=prev["token"],
            laboratory_id=lab.id,
            city_id=c1.id,
            generate_ai=False,
        )

    assert comm["created"] == 2

    # Verify DB state
    c1_tests = LabService.query.filter_by(laboratory_id=lab.id, city_id=c1.id).all()
    c2_tests = LabService.query.filter_by(laboratory_id=lab.id, city_id=c2.id).all()

    assert len(c1_tests) == 2
    assert len(c2_tests) == 0


def test_reimport_unchanged_file_zero_pending(sample_labs_and_cities):
    """4. Test that re-importing the exact same Excel file creates 0 pending tests and 0 AI calls."""
    lab = sample_labs_and_cities["lab_a"]
    c1 = sample_labs_and_cities["city_a1"]

    excel_file = _create_excel_bytes([
        ["Test Name", "Price", "Test Details"],
        ["Urea", "90.00", "Kidney test"],
        ["Creatinine", "100.00", "Kidney test"],
    ])

    # First import
    prev1 = preview_import(excel_file, "prices.xlsx", lab.id, c1.id)
    with patch("services.domain.price_import_service.upsert_test_vectors_batch"):
        comm1 = commit_import(prev1["token"], lab.id, c1.id, generate_ai=False)

    assert comm1["created"] == 2

    # Manually mark tests as 'ok' (simulating background AI completion)
    LabService.query.filter_by(city_id=c1.id).update({"sync_status": "ok"})
    db.session.commit()

    # Re-import identical file
    excel_file.seek(0)
    prev2 = preview_import(excel_file, "prices.xlsx", lab.id, c1.id)

    assert prev2["new"] == 0
    assert prev2["price_changed"] == 0
    assert prev2["desc_changed"] == 0
    assert prev2["unchanged"] == 2

    with patch("services.domain.price_import_service.generate_test") as mock_ai:
        with patch("services.domain.price_import_service.upsert_test_vectors_batch"):
            comm2 = commit_import(prev2["token"], lab.id, c1.id, generate_ai=True)

    assert comm2["created"] == 0
    assert comm2["updated"] == 0
    assert comm2["total_for_ai"] == 0
    mock_ai.assert_not_called()


def test_zero_or_invalid_price_skipped(sample_labs_and_cities):
    """5. Test price parsing for 1,105.00, Arabic-Indic digits, and non-numeric or <=0 prices going to needs_review."""
    # Test helper directly
    assert _cell_float("1,105.00") == 1105.0
    assert _cell_float("١١٠٥.٠٠") == 1105.0
    assert _cell_float(" 1 500 ") == 1500.0
    assert _cell_float("0") is None
    assert _cell_float("-50") is None
    assert _cell_float("N/A") is None

    # Test preview behavior
    lab = sample_labs_and_cities["lab_a"]
    c1 = sample_labs_and_cities["city_a1"]

    excel_file = _create_excel_bytes([
        ["Test Name", "Price"],
        ["Valid Test", "1,200.00"],
        ["Zero Price Test", "0"],
        ["Invalid Text Price", "Call Lab"],
        ["Negative Price Test", "-100"],
    ])

    prev = preview_import(excel_file, "prices.xlsx", lab.id, c1.id)
    assert prev["new"] == 1
    assert len(prev["needs_review"]) == 3

    reasons = [r["reason"] for r in prev["needs_review"]]
    assert any("0" in r for r in reasons)
    assert any("Call Lab" in r for r in reasons)


def test_duplicate_name_in_file_last_wins(sample_labs_and_cities):
    """6. Test that duplicate test names inside the same file keep the last row's price and emit a warning."""
    lab = sample_labs_and_cities["lab_a"]
    c1 = sample_labs_and_cities["city_a1"]

    excel_file = _create_excel_bytes([
        ["Test Name", "Price"],
        ["Uric Acid", "80.00"],
        ["Uric Acid", "95.00"],  # Last row wins
    ])

    prev = preview_import(excel_file, "prices.xlsx", lab.id, c1.id)
    assert prev["new"] == 1
    assert len(prev["warnings"]) == 1
    assert "اسم مكرر في الملف" in prev["warnings"][0]

    with patch("services.domain.price_import_service.upsert_test_vectors_batch"):
        comm = commit_import(prev["token"], lab.id, c1.id, generate_ai=False)

    assert comm["created"] == 1
    test_in_db = LabService.query.filter_by(city_id=c1.id, name="Uric Acid").first()
    assert float(test_in_db.price) == 95.00


def test_delete_missing_only_affects_selected_city(sample_labs_and_cities):
    """7. Test that delete_missing=True deletes unmentioned tests ONLY in the selected city."""
    lab = sample_labs_and_cities["lab_a"]
    c1 = sample_labs_and_cities["city_a1"]
    c2 = sample_labs_and_cities["city_a2"]

    # Seed tests in both cities
    ts = TestsService(laboratory_id=lab.id)
    with patch("services.domain.tests_service.upsert_test_vector"):
        ts.create_lab_service(lab.id, c1.id, "Cairo Only Test", 100.0)
        ts.create_lab_service(lab.id, c1.id, "Keep Test", 200.0)
        ts.create_lab_service(lab.id, c2.id, "Cairo Only Test", 100.0)  # Same name in City 2

    excel_file = _create_excel_bytes([
        ["Test Name", "Price"],
        ["Keep Test", "200.00"],
    ])

    prev = preview_import(excel_file, "prices.xlsx", lab.id, c1.id, delete_missing=True)
    assert prev["would_delete"]["count"] == 1
    assert "Cairo Only Test" in prev["would_delete"]["names"]

    with patch("services.domain.price_import_service.upsert_test_vectors_batch"):
        with patch("services.domain.price_import_service.delete_test_vector"):
            comm = commit_import(prev["token"], lab.id, c1.id, delete_missing=True, generate_ai=False)

    assert comm["deleted"] == 1

    # Verify City 1 test was deleted, but City 2 test remains intact
    assert LabService.query.filter_by(city_id=c1.id, name="Cairo Only Test").first() is None
    assert LabService.query.filter_by(city_id=c2.id, name="Cairo Only Test").first() is not None


def test_embedding_failure_keeps_pending_status(sample_labs_and_cities):
    """8. Test that vector upsert failure sets sync_status='pending' and surfaces a user warning."""
    lab = sample_labs_and_cities["lab_a"]
    c1 = sample_labs_and_cities["city_a1"]
    ts = TestsService(laboratory_id=lab.id)

    with patch("services.domain.tests_service.upsert_test_vector", side_effect=Exception("Qdrant offline")):
        test_obj, msg = ts.create_lab_service(lab.id, c1.id, "Vector Fail Test", 300.0)

    assert test_obj is not None
    assert test_obj.sync_status == "pending"
    assert "فشل تحديث الفهرس" in msg


def test_tenant_isolation_routes(sample_labs_and_cities, test_app):
    """9. Test that user from Laboratory A cannot modify Laboratory B's cities/branches/tests."""
    lab_a = sample_labs_and_cities["lab_a"]
    lab_b = sample_labs_and_cities["lab_b"]
    c_b1 = sample_labs_and_cities["city_b1"]

    cs = CityService()
    city, msg = cs.update_city(city_id=c_b1.id, name="Hacked Name", laboratory_id=lab_a.id)

    assert city is None
    assert "صلاحية" in msg or "غير موجودة" in msg


def test_invalid_or_expired_preview_token(sample_labs_and_cities):
    """10. Test that commit_import with an invalid or expired token returns a clear error."""
    lab = sample_labs_and_cities["lab_a"]
    c1 = sample_labs_and_cities["city_a1"]

    result = commit_import(token="invalid_expired_token_123", laboratory_id=lab.id, city_id=c1.id)
    assert result["error"] is not None
    assert "رمز المعاينة غير صالح" in result["error"]
