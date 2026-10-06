from flask import Blueprint, request, jsonify, flash, redirect, url_for
from flask_login import login_required
from services.domain.branch_service import BranchService

branch_bp = Blueprint("branch", __name__, url_prefix="/branches")


@branch_bp.route("/create", methods=["POST"])
@login_required
def create_branch():
    is_ajax = request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form

    city_id = data.get("city_id", type=int) if not request.is_json else data.get("city_id")
    name = data.get("name")
    address = data.get("address")
    phone_number = data.get("phone_number")
    working_hours = data.get("working_hours")
    laboratory_id = data.get("laboratory_id") or 1

    branch_service = BranchService(laboratory_id=laboratory_id)
    branch, message = branch_service.create_branch(
        city_id=int(city_id) if city_id else None,
        name=name,
        address=address,
        phone_number=phone_number,
        working_hours=working_hours,
        laboratory_id=int(laboratory_id) if laboratory_id else 1,
    )

    if is_ajax:
        if branch:
            return jsonify({
                "success": True,
                "message": message,
                "branch": {
                    "id": branch.id,
                    "city_id": branch.city_id,
                    "name": branch.name,
                    "address": branch.address,
                    "working_hours": branch.working_hours,
                }
            })
        return jsonify({"success": False, "error": message}), 400

    flash(message, "success" if branch else "error")
    return redirect(request.referrer or url_for("laboratory.list_laboratories"))


@branch_bp.route("/<int:branch_id>/edit", methods=["POST"])
@login_required
def edit_branch(branch_id):
    is_ajax = request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form

    laboratory_id = data.get("laboratory_id") or 1
    branch_service = BranchService(branch_id=branch_id, laboratory_id=laboratory_id)

    branch, message = branch_service.update_branch(
        branch_id=branch_id,
        name=data.get("name"),
        address=data.get("address"),
        phone_number=data.get("phone_number"),
        working_hours=data.get("working_hours"),
        laboratory_id=int(laboratory_id) if laboratory_id else 1,
    )

    if is_ajax:
        if branch:
            return jsonify({"success": True, "message": message})
        return jsonify({"success": False, "error": message}), 400

    flash(message, "success" if branch else "error")
    return redirect(request.referrer or url_for("laboratory.list_laboratories"))


@branch_bp.route("/<int:branch_id>/delete", methods=["POST"])
@login_required
def delete_branch(branch_id):
    is_ajax = request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    data = request.get_json(silent=True) if request.is_json else request.form
    laboratory_id = data.get("laboratory_id") or 1

    branch_service = BranchService(branch_id=branch_id, laboratory_id=laboratory_id)
    success, message = branch_service.delete_branch(branch_id=branch_id, laboratory_id=int(laboratory_id) if laboratory_id else 1)

    if is_ajax:
        if success:
            return jsonify({"success": True, "message": message})
        return jsonify({"success": False, "error": message}), 400

    flash(message, "success" if success else "error")
    return redirect(request.referrer or url_for("laboratory.list_laboratories"))
