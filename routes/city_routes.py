from flask import Blueprint, request, jsonify, flash, redirect, url_for
from flask_login import login_required
from services.domain.city_service import CityService

city_bp = Blueprint("city", __name__, url_prefix="/cities")


@city_bp.route("/create", methods=["POST"])
@login_required
def create_city():
    laboratory_id = request.form.get("laboratory_id", type=int) or 1
    name = request.form.get("name", "").strip()
    sort_order = request.form.get("sort_order", 0, type=int)

    city_service = CityService()
    city, message = city_service.create_city(
        laboratory_id=laboratory_id,
        name=name,
        sort_order=sort_order
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
    laboratory_id = request.form.get("laboratory_id", type=int) or request.json.get("laboratory_id") if request.is_json else 1
    name = request.form.get("name") if not request.is_json else request.json.get("name")
    sort_order = request.form.get("sort_order") if not request.is_json else request.json.get("sort_order")

    city_service = CityService(city_id=city_id, laboratory_id=laboratory_id)
    city, message = city_service.update_city(
        city_id=city_id,
        name=name,
        sort_order=sort_order,
        laboratory_id=laboratory_id
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
    laboratory_id = data.get("laboratory_id", 1)
    orders = data.get("orders", [])  # list of [city_id, new_sort_order]

    success, message = CityService.reorder_cities(laboratory_id=laboratory_id, city_orders=orders)
    if success:
        return jsonify({"success": True, "message": message})
    return jsonify({"success": False, "error": message}), 400
