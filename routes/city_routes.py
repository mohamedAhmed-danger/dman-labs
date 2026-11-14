from flask import Blueprint, request, jsonify, flash, redirect, url_for
from flask_login import login_required
from services.domain.city_service import CityService

city_bp = Blueprint("city", __name__, url_prefix="/cities")


@city_bp.route("/create", methods=["POST"])
@login_required
def create_city():
    if request.is_json:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            data = {}
        laboratory_id = data.get("laboratory_id")
        name = data.get("name", "")
        sort_order = data.get("sort_order", 0)
        aliases = data.get("aliases")
    else:
        data = request.form
        laboratory_id = data.get("laboratory_id")
        name = data.get("name", "")
        sort_order = data.get("sort_order", 0)
        aliases = data.get("aliases")

    try:
        laboratory_id = int(laboratory_id)
        sort_order = int(sort_order or 0)
    except (TypeError, ValueError):
        message = "بيانات المعمل أو ترتيب المنطقة غير صالحة"
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "error": message}), 400
        flash(message, "error")
        return redirect(request.referrer or url_for("laboratory.list_laboratories"))
    name = name.strip() if isinstance(name, str) else ""

    city_service = CityService()
    city, message = city_service.create_city(
        laboratory_id=laboratory_id,
        name=name,
        sort_order=sort_order,
        aliases=aliases,
    )

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        if city:
            return jsonify({"success": True, "message": message, "city": {"id": city.id, "name": city.name}})
        return jsonify({"success": False, "error": message}), 400

    flash(message, "success" if city else "error")
    return redirect(request.referrer or url_for("laboratory.list_laboratories"))


@city_bp.route("/<int:city_id>/edit", methods=["POST"])
@login_required
def edit_city(city_id):
    if request.is_json:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            data = {}
        laboratory_id = data.get("laboratory_id")
    else:
        data = request.form
        laboratory_id = data.get("laboratory_id", type=int)
    name = data.get("name")
    sort_order = data.get("sort_order")
    aliases = data.get("aliases")
    try:
        laboratory_id = int(laboratory_id)
        if sort_order == "":
            sort_order = None
        elif sort_order is not None:
            sort_order = int(sort_order)
    except (TypeError, ValueError):
        message = "بيانات المعمل أو المنطقة غير صالحة"
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "error": message}), 400
        flash(message, "error")
        return redirect(request.referrer or url_for("laboratory.list_laboratories"))

    if not laboratory_id:
        message = "معرّف المعمل غير صالح"
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "error": message}), 400
        flash(message, "error")
        return redirect(request.referrer or url_for("laboratory.list_laboratories"))

    city_service = CityService(city_id=city_id, laboratory_id=laboratory_id)
    city, message = city_service.update_city(
        city_id=city_id,
        name=name,
        sort_order=sort_order,
        laboratory_id=laboratory_id,
        aliases=aliases,
    )

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        if city:
            return jsonify({"success": True, "message": message})
        return jsonify({"success": False, "error": message}), 400

    flash(message, "success" if city else "error")
    return redirect(request.referrer or url_for("laboratory.list_laboratories"))


@city_bp.route("/<int:city_id>/delete", methods=["POST"])
@login_required
def delete_city(city_id):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    laboratory_id = data.get("laboratory_id") or request.form.get("laboratory_id", type=int) or 1

    city_service = CityService(city_id=city_id, laboratory_id=laboratory_id)
    success, message = city_service.delete_city(city_id=city_id, laboratory_id=laboratory_id)

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        if success:
            return jsonify({"success": True, "message": message})
        return jsonify({"success": False, "error": message}), 400

    flash(message, "success" if success else "error")
    return redirect(request.referrer or url_for("laboratory.list_laboratories"))


@city_bp.route("/reorder", methods=["POST"])
@login_required
def reorder_cities():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "بيانات الترتيب غير صحيحة"}), 400
    try:
        laboratory_id = int(data.get("laboratory_id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "معرّف المعمل غير صالح"}), 400
    orders = data.get("orders", [])

    success, message = CityService.reorder_cities(laboratory_id=laboratory_id, city_orders=orders)
    if success:
        return jsonify({"success": True, "message": message})
    return jsonify({"success": False, "error": message}), 400
