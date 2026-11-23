from flask import Blueprint, request, render_template, redirect, url_for, flash, jsonify, send_file
from flask_login import login_required
from services.domain.tests_service import TestsService
from services.domain.city_service import CityService
from models.models import db, LabService, City
from services.domain.price_export_service import export_test_prices
from services.domain.price_import_service import (
    MAX_IMPORT_FILE_SIZE,
    commit_import,
    get_city_import_progress,
    preview_import,
)

test_bp = Blueprint("test", __name__)


@test_bp.route("/test_service", methods=["GET"])
@login_required
def test_service():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 10, type=int)
    search = request.args.get("search", "").strip()
    laboratory_id = request.args.get("laboratory_id", 1, type=int)
    sync_status = request.args.get("sync_status") or request.args.get("status")

    cities = CityService.get_cities_by_laboratory(laboratory_id)

    # The catalog is uniform across regions: no city filter on this page.
    raw_city_id = None
    selected_city_id = None

    tests_service = TestsService(laboratory_id=laboratory_id, city_id=selected_city_id)
    pagination, services, laboratories, message = tests_service.get_test_service_page_data(
        page=page, per_page=per_page, search=search, laboratory_id=laboratory_id,
        city_id=selected_city_id, sync_status=sync_status
    )

    if pagination is None and message:
        flash(message, "error")

    # The sync status belongs to the test itself (one vector per test), not to a city
    pending_count = LabService.query.filter_by(
        laboratory_id=laboratory_id, sync_status="pending"
    ).count()

    return render_template(
        "test/test_service.html",
        services=services,
        pagination=pagination,
        search=search,
        page=page,
        per_page=per_page,
        laboratory_id=laboratory_id,
        laboratories=laboratories,
        cities=cities,
        selected_city_id=selected_city_id,
        raw_city_id=raw_city_id,
        sync_status=sync_status,
        pending_count=pending_count,
    )

test_service.__test__ = False


@test_bp.route("/tests/export", methods=["GET"])
@login_required
def export_test_prices_route():
    raw_city_id = request.args.get("city_id", "all").strip().lower()
    laboratory_id = request.args.get("laboratory_id", 1, type=int) or 1
    if raw_city_id == "all":
        city_id = None
    elif raw_city_id.isdigit() and int(raw_city_id) > 0:
        city_id = int(raw_city_id)
    else:
        return jsonify({"error": "يرجى اختيار منطقة صحيحة للتصدير"}), 400

    try:
        workbook, filename = export_test_prices(laboratory_id, city_id)
    except ValueError as error:
        return jsonify({"error": str(error)}), 404
    except Exception:
        return jsonify({"error": "تعذر إنشاء ملف التصدير"}), 500

    return send_file(
        workbook,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@test_bp.route("/tests/import/preview", methods=["POST"])
@login_required
def import_test_prices_preview():
    if request.content_length and request.content_length > MAX_IMPORT_FILE_SIZE + 65536:
        return jsonify({"error": "حجم الملف أكبر من الحد المسموح (16 ميجابايت)"}), 413
    uploaded_file = request.files.get("file")
    if not uploaded_file or not uploaded_file.filename:
        return jsonify({"error": "يرجى اختيار ملف أسعار"}), 400

    city_id = request.form.get("city_id", type=int)
    if not city_id:
        return jsonify({"error": "يرجى اختيار المنطقة المستهدفة"}), 400
    city = db.session.get(City, city_id)
    if not city:
        return jsonify({"error": "المنطقة غير موجودة"}), 404

    delete_missing = request.form.get("delete_missing", "false").lower() in ("true", "1", "yes", "on")
    try:
        preview = preview_import(
            file_obj=uploaded_file,
            filename=uploaded_file.filename,
            laboratory_id=city.laboratory_id,
            city_id=city.id,
            delete_missing=delete_missing,
        )
        if preview.get("error"):
            status = 413 if "حجم الملف" in preview["error"] else 400
            return jsonify({"error": preview["error"]}), status
        return jsonify(preview)
    except Exception as error:
        return jsonify({"error": f"حدث خطأ أثناء معاينة الملف: {error}"}), 500


@test_bp.route("/tests/import/commit", methods=["POST"])
@login_required
def import_test_prices_commit():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    try:
        city_id = int(data.get("city_id"))
    except (TypeError, ValueError):
        city_id = None
    if not token or not city_id:
        return jsonify({"error": "بيانات التأكيد غير مكتملة"}), 400
    city = db.session.get(City, city_id)
    if not city:
        return jsonify({"error": "المنطقة غير موجودة"}), 404

    try:
        result = commit_import(
            token=token,
            city_id=city.id,
            delete_missing=str(data.get("delete_missing", False)).lower() in ("true", "1", "yes", "on"),
            generate_ai=str(data.get("generate_ai", True)).lower() in ("true", "1", "yes", "on"),
        )
        if result.get("error"):
            return jsonify({"error": result["error"]}), 400
        return jsonify(result)
    except Exception as error:
        db.session.rollback()
        return jsonify({"error": f"حدث خطأ أثناء تنفيذ الاستيراد: {error}"}), 500


@test_bp.route("/tests/import/status", methods=["GET"])
@login_required
def import_test_prices_status():
    city_id = request.args.get("city_id", type=int)
    city = db.session.get(City, city_id) if city_id else None
    if not city:
        return jsonify({"error": "المنطقة غير موجودة"}), 404

    progress = get_city_import_progress(city.id)
    if progress:
        return jsonify({key: int(progress.get(key, 0)) for key in ("total", "done", "failed", "pending")})

    totals = db.session.query(LabService.sync_status, db.func.count(LabService.id)).filter_by(
        laboratory_id=city.laboratory_id
    ).group_by(LabService.sync_status).all()
    counts = dict(totals)
    pending = counts.get("pending", 0)
    failed = counts.get("failed", 0)
    done = counts.get("ok", 0)
    return jsonify({"total": done + failed + pending, "done": done, "failed": failed, "pending": pending})


@test_bp.route("/tests/search", methods=["GET"])
@test_bp.route("/api/tests/search", methods=["GET"])
@login_required
def search_tests():
    query = request.args.get("q", "").strip()
    limit = min(request.args.get("limit", 15, type=int), 30)
    laboratory_id = request.args.get("laboratory_id", type=int)
    city_id = request.args.get("city_id", type=int)
    if not laboratory_id or not city_id:
        return jsonify({"error": "laboratory_id and city_id are required"}), 400
    city = db.session.get(City, city_id)
    if not city or city.laboratory_id != laboratory_id:
        return jsonify({"error": "city_id does not belong to laboratory_id"}), 400

    tests_service = TestsService(laboratory_id=laboratory_id, city_id=city_id)
    results = tests_service.search_services(query=query, limit=limit, laboratory_id=laboratory_id, city_id=city_id)
    return jsonify(results)


@test_bp.route("/tests/create", methods=["GET", "POST"])
@login_required
def create_test():
    laboratory_id = request.args.get("laboratory_id", 1, type=int)
    tests_service = TestsService(laboratory_id=laboratory_id)
    cities = CityService.get_cities_by_laboratory(laboratory_id)

    if request.method == "GET":
        laboratories = tests_service.get_all_laboratories()
        return render_template(
            "test/test_form.html",
            laboratories=laboratories,
            cities=cities,
            city_prices={},
            selected_city_id=request.args.get("city_id", type=int),
            is_edit=False
        )

    success, message = tests_service.handle_create_test(request.form)
    flash(message, "success" if success else "error")

    if success:
        return redirect(url_for("test.test_service",
                                city_id=request.args.get("city_id") or "all",
                                laboratory_id=laboratory_id))
    return redirect(url_for("test.create_test", laboratory_id=laboratory_id))


@test_bp.route("/tests/<int:test_id>/edit", methods=["GET", "POST"])
@login_required
def edit_test(test_id):
    tests_service = TestsService(test_id=test_id)
    service, message = tests_service.get_test()
    laboratories = tests_service.get_all_laboratories()

    if not service:
        flash(message, "error")
        return redirect(url_for("test.test_service"))

    cities = CityService.get_cities_by_laboratory(service.laboratory_id)

    if request.method == "GET":
        return render_template(
            "test/test_form.html",
            service=service,
            laboratories=laboratories,
            cities=cities,
            city_prices={cp.city_id: cp.price for cp in service.city_prices},
            selected_city_id=request.args.get("city_id", type=int),
            is_edit=True
        )

    success, message = tests_service.handle_update_test(request.form)
    flash(message, "success" if success else "error")

    if success:
        return redirect(url_for("test.test_service",
                                city_id=request.args.get("city_id") or "all",
                                laboratory_id=service.laboratory_id))
    return redirect(url_for("test.edit_test", test_id=test_id))


@test_bp.route("/tests/<int:test_id>/delete", methods=["POST"])
@login_required
def delete_test(test_id):
    tests_service = TestsService(test_id=test_id)
    test_obj, _ = tests_service.get_test()
    lab_id = test_obj.laboratory_id if test_obj else 1
    c_id = request.form.get("city_id") or request.args.get("city_id") or "all"
    service, message = tests_service.delete_test()
    flash(message, "success" if service else "error")
    return redirect(url_for("test.test_service", city_id=c_id, laboratory_id=lab_id))


@test_bp.route("/tests/generate", methods=["POST"])
@login_required
def generate():
    tests_service = TestsService()
    is_ajax = tests_service.is_ajax_request(request)
    result, error_msg = tests_service.process_ai_generation(request)

    if error_msg:
        if is_ajax:
            return jsonify({"error": error_msg}), 400
        flash(error_msg, "error")
        return redirect(url_for("test.create_test"))

    if is_ajax:
        return jsonify(tests_service.format_ai_response(result))

    laboratories = tests_service.get_all_laboratories()
    cities = CityService.get_cities_by_laboratory(1)
    return render_template(
        "test/test_form.html",
        generated=result,
        form_data=request.form,
        laboratories=laboratories,
        cities=cities,
        city_prices={},
        is_edit=False
    )


@test_bp.route("/tests/regenerate", methods=["POST"])
@login_required
def regenerate():
    tests_service = TestsService()
    is_ajax = tests_service.is_ajax_request(request)
    result, error_msg = tests_service.process_ai_regeneration(request)

    if error_msg:
        if is_ajax:
            return jsonify({"error": error_msg}), 400
        flash(error_msg, "error")
        return redirect(url_for("test.create_test"))

    if is_ajax:
        return jsonify(tests_service.format_ai_response(result))

    laboratories = tests_service.get_all_laboratories()
    cities = CityService.get_cities_by_laboratory(1)
    return render_template(
        "test/test_form.html",
        generated=result,
        form_data=request.form,
        laboratories=laboratories,
        cities=cities,
        city_prices={},
        is_edit=False
    )