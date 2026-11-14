import io

import openpyxl
import pytest
from flask import Flask
from flask_login import LoginManager

from models.models import City, LabService, Laboratory, User, db
from routes import test_routes
from services.domain import price_import_service


class TestConfig:
    SECRET_KEY = "test-secret"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {}
    TESTING = True


@pytest.fixture
def api_client(monkeypatch):
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.from_object(TestConfig)
    db.init_app(app)
    login_manager = LoginManager()
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    app.register_blueprint(test_routes.test_bp)
    template_endpoints = (
        "main.dashboard",
        "main.logout",
        "inquiries.list_inquiries",
        "bookings.list_bookings",
        "complaints.list_complaints",
        "bundle.list_bundles",
        "feedbacks.list_feedbacks_page",
        "laboratory.list_laboratories",
        "platforms.list_platforms",
        "pages.list_pages",
        "users.list_users",
        "gmail_auth.settings_page",
    )
    for endpoint in template_endpoints:
        app.add_url_rule(
            f"/__test/{endpoint.replace('.', '/')}",
            endpoint=endpoint,
            view_func=lambda: "",
        )

    with app.app_context():
        db.create_all()
        laboratory = Laboratory(name="Test Lab", location="Egypt")
        db.session.add(laboratory)
        db.session.flush()
        city = City(laboratory_id=laboratory.id, name="الإسماعيلية")
        user = User(name="admin", password="test")
        db.session.add_all([city, user])
        db.session.commit()
        city_id = city.id
        user_id = user.id

        with app.test_client() as client:
            with client.session_transaction() as session:
                session["_user_id"] = str(user_id)
                session["_fresh"] = True
            yield client, city_id

        db.session.remove()
        db.drop_all()


def test_parser_flags_bad_rows_and_preserves_arabic(api_client, monkeypatch):
    _, city_id = api_client
    csv_data = "#,Test Name,Price\r\n1,تحليل صورة الدم,120\r\n2,,40\r\n3,تحليل غير صالح,نص\r\n4,تحليل سالب,-2\r\n5,تحليل صفر,0\r\n"
    saved_preview = {}
    monkeypatch.setattr(price_import_service, "_save_preview", lambda token, data: saved_preview.update(data))
    city = db.session.get(City, city_id)

    result = price_import_service.preview_import(
        io.BytesIO(csv_data.encode("utf-8-sig")),
        "prices.csv",
        city.laboratory_id,
        city.id,
    )

    assert result["new"] == 1
    assert saved_preview["rows"][0]["name"] == "تحليل صورة الدم"
    assert [item["row"] for item in result["needs_review"]] == [3, 4, 5, 6]
    assert result["needs_review"][0]["reason"] == "اسم التحليل فارغ"
    assert "نص" in result["needs_review"][1]["reason"]

    wrong_extension = price_import_service.preview_import(
        io.BytesIO(b"data"), "prices.txt", city.laboratory_id, city.id
    )
    empty_file = price_import_service.preview_import(
        io.BytesIO(b""), "empty.xlsx", city.laboratory_id, city.id
    )
    assert "غير مدعوم" in wrong_extension["error"]
    assert empty_file["error"]


def test_delete_missing_preview_does_not_write_until_commit(api_client, monkeypatch):
    _, city_id = api_client
    city = db.session.get(City, city_id)
    db.session.add_all([
        LabService(laboratory_id=city.laboratory_id, city_id=city_id, name=name, price=10)
        for name in ("Keep", "Remove A", "Remove B", "Remove C")
    ])
    db.session.commit()

    saved = {}
    progress_store = {}

    class FakeRedis:
        def set(self, key, value, **kwargs):
            progress_store[key] = value

        def hset(self, key, field=None, value=None, mapping=None):
            progress_store[key] = dict(mapping or {})
            if field is not None:
                progress_store.setdefault(key, {})[field] = value

        def expire(self, *args, **kwargs):
            return True

    monkeypatch.setattr(price_import_service, "_save_preview", lambda token, data: saved.update({token: data}))
    monkeypatch.setattr(price_import_service, "_load_preview", lambda token: saved.get(token))
    monkeypatch.setattr(price_import_service, "get_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(price_import_service, "delete_test_vector", lambda *args, **kwargs: None)

    preview = price_import_service.preview_import(
        io.BytesIO(b"#,Test Name,Price\n1,Keep,10\n"),
        "prices.csv",
        city.laboratory_id,
        city_id,
        delete_missing=True,
    )
    assert preview["would_delete"] == {"count": 3, "names": ["Remove A", "Remove B", "Remove C"]}
    assert LabService.query.filter_by(city_id=city_id).count() == 4

    committed = price_import_service.commit_import(
        preview["token"], city_id, delete_missing=True, generate_ai=False
    )
    assert committed["deleted"] == 3
    assert [test.name for test in LabService.query.filter_by(city_id=city_id).all()] == ["Keep"]


def test_export_route_returns_three_column_xlsx_and_reimports_unchanged(api_client, monkeypatch):
    client, city_id = api_client
    city = db.session.get(City, city_id)
    db.session.add(LabService(
        laboratory_id=city.laboratory_id,
        city_id=city.id,
        name="تحليل صورة الدم",
        price=125.5,
    ))
    db.session.commit()
    monkeypatch.setattr(price_import_service, "_save_preview", lambda token, data: None)

    response = client.get(f"/tests/export?city_id={city_id}")
    workbook = openpyxl.load_workbook(io.BytesIO(response.data), data_only=True)
    rows = list(workbook.active.values)
    assert response.status_code == 200
    assert rows[0] == ("#", "Test Name", "Price")
    assert rows[1] == (1, "تحليل صورة الدم", 125.5)

    preview = client.post("/tests/import/preview", data={
        "city_id": str(city_id),
        "file": (io.BytesIO(response.data), "exported.xlsx"),
    }, content_type="multipart/form-data")
    assert preview.status_code == 200
    assert preview.json["created"] == 0
    assert preview.json["updated"] == 0
    assert preview.json["unchanged"] == 1


def test_preview_route_counts_two_updates_and_one_creation(api_client, monkeypatch):
    client, city_id = api_client
    city = db.session.get(City, city_id)
    db.session.add_all([
        LabService(laboratory_id=city.laboratory_id, city_id=city_id, name="A", price=10),
        LabService(laboratory_id=city.laboratory_id, city_id=city_id, name="B", price=20),
    ])
    db.session.commit()
    monkeypatch.setattr(price_import_service, "_save_preview", lambda token, data: None)

    response = client.post("/tests/import/preview", data={
        "city_id": str(city_id),
        "file": (io.BytesIO(b"#,Test Name,Price\n1,A,11\n2,B,22\n3,C,30\n"), "prices.csv"),
    }, content_type="multipart/form-data")

    assert response.status_code == 200
    assert response.json["created"] == 1
    assert response.json["updated"] == 2
    assert response.json["unchanged"] == 0


def test_preview_route_returns_clear_bad_file_errors(api_client, monkeypatch):
    client, city_id = api_client
    monkeypatch.setattr(price_import_service, "_save_preview", lambda token, data: None)

    wrong_extension = client.post("/tests/import/preview", data={
        "city_id": str(city_id),
        "file": (io.BytesIO(b"bad"), "prices.txt"),
    }, content_type="multipart/form-data")
    empty_file = client.post("/tests/import/preview", data={
        "city_id": str(city_id),
        "file": (io.BytesIO(b""), "empty.csv"),
    }, content_type="multipart/form-data")
    invalid_price = client.post("/tests/import/preview", data={
        "city_id": str(city_id),
        "file": (io.BytesIO(b"#,Test Name,Price\n1,Test,N/A\n"), "prices.csv"),
    }, content_type="multipart/form-data")

    assert wrong_extension.status_code == 400
    assert wrong_extension.json["error"]
    assert empty_file.status_code == 400
    assert empty_file.json["error"]
    assert invalid_price.status_code == 200
    assert invalid_price.json["needs_review"][0]["row"] == 2


def test_preview_route_returns_preview_contract(api_client, monkeypatch):
    client, city_id = api_client
    monkeypatch.setattr(test_routes, "preview_import", lambda **kwargs: {
        "token": "preview-token", "new": 1, "price_changed": 2, "unchanged": 3,
        "needs_review": [], "would_delete": {"count": 0, "names": []}, "error": None,
    })

    response = client.post("/tests/import/preview", data={
        "city_id": str(city_id),
        "generate_ai": "true",
        "delete_missing": "false",
        "file": (io.BytesIO(b"#,Test Name,Price\n"), "prices.csv"),
    }, content_type="multipart/form-data")

    assert response.status_code == 200
    assert response.json["created"] == 1
    assert response.json["updated"] == 2
    assert response.json["unchanged"] == 3
    assert response.json["token"] == "preview-token"


def test_commit_route_forwards_options(api_client, monkeypatch):
    client, city_id = api_client
    captured = {}

    def commit(**kwargs):
        captured.update(kwargs)
        return {"created": 1, "updated": 0, "token": kwargs["token"]}

    monkeypatch.setattr(test_routes, "commit_import", commit)
    response = client.post("/tests/import/commit", json={
        "token": "preview-token", "city_id": city_id,
        "delete_missing": True, "generate_ai": False,
    })

    assert response.status_code == 200
    assert captured == {
        "token": "preview-token", "city_id": city_id,
        "delete_missing": True, "generate_ai": False,
    }


def test_status_route_returns_city_progress(api_client, monkeypatch):
    client, city_id = api_client
    monkeypatch.setattr(test_routes, "get_city_import_progress", lambda value: {
        "total": 8, "done": 4, "failed": 1, "pending": 3,
    })

    response = client.get(f"/tests/import/status?city_id={city_id}")

    assert response.status_code == 200
    assert response.json == {"total": 8, "done": 4, "failed": 1, "pending": 3}


@pytest.mark.parametrize("query", [
    "",
    "?city_id=all",
    "?city_id={city_id}",
    "?search=abc",
    "?sync_status=pending",
])
def test_test_service_page_renders_for_query_variants(api_client, monkeypatch, query):
    client, city_id = api_client
    monkeypatch.setattr(
        test_routes.TestsService,
        "get_test_service_page_data",
        lambda self, **kwargs: (None, [], [], None),
    )
    url = "/test_service" + query.format(city_id=city_id)

    response = client.get(url)

    assert response.status_code == 200
    assert "خدمات التحاليل الطبية".encode("utf-8") in response.data
    assert b'id="importModal"' in response.data