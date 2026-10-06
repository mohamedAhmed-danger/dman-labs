from flask import Blueprint, request, render_template, redirect, url_for, flash, jsonify
from flask_login import login_required
from services.domain.tests_service import TestsService
from services.domain.city_service import CityService
from models.models import db, LabService, City

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

    raw_city_id = request.args.get("city_id")
    if raw_city_id == "all" or raw_city_id == "0":
        selected_city_id = None
    elif raw_city_id is not None and raw_city_id.isdigit():
        selected_city_id = int(raw_city_id)
    else:
        # Default to first city by sort_order
        selected_city_id = cities[0].id if cities else None

    tests_service = TestsService(laboratory_id=laboratory_id, city_id=selected_city_id)
    pagination, services, laboratories, message = tests_service.get_test_service_page_data(
        page=page, per_page=per_page, search=search, laboratory_id=laboratory_id,
        city_id=selected_city_id, sync_status=sync_status
    )

    if pagination is None and message:
        flash(message, "error")

    # Count pending tests for current laboratory & selected city (or all cities)
    pending_query = LabService.query.filter_by(laboratory_id=laboratory_id, sync_status="pending")
    if selected_city_id:
        pending_query = pending_query.filter_by(city_id=selected_city_id)
    pending_count = pending_query.count()

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


@test_bp.route("/tests/search", methods=["GET"])
@test_bp.route("/api/tests/search", methods=["GET"])
@login_required
def search_tests():
    query = request.args.get("q", "").strip()
    limit = min(request.args.get("limit", 15, type=int), 30)
    laboratory_id = request.args.get("laboratory_id", type=int)
    city_id = request.args.get("city_id", type=int)

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
        selected_city_id = request.args.get("city_id", type=int) or (cities[0].id if cities else None)
        return render_template(
            "test/test_form.html",
            laboratories=laboratories,
            cities=cities,
            selected_city_id=selected_city_id,
            is_edit=False
        )

    success, message = tests_service.handle_create_test(request.form)
    flash(message, "success" if success else "error")

    if success:
        c_id = request.form.get("city_id")
        return redirect(url_for("test.test_service", city_id=c_id))
    return redirect(url_for("test.create_test"))


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
            selected_city_id=service.city_id,
            is_edit=True
        )

    success, message = tests_service.handle_update_test(request.form)
    flash(message, "success" if success else "error")

    if success:
        return redirect(url_for("test.test_service", city_id=service.city_id))
    return redirect(url_for("test.edit_test", test_id=test_id))


@test_bp.route("/tests/<int:test_id>/delete", methods=["POST"])
@login_required
def delete_test(test_id):
    tests_service = TestsService(test_id=test_id)
    test_obj, _ = tests_service.get_test()
    c_id = test_obj.city_id if test_obj else None
    service, message = tests_service.delete_test()
    flash(message, "success" if service else "error")
    return redirect(url_for("test.test_service", city_id=c_id))


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
        is_edit=False
    )