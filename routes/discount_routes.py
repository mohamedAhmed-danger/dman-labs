from flask import Blueprint, render_template, request, redirect, url_for, flash
from models.models import City
from services.domain.discount_service import DiscountService

discount_bp = Blueprint("discount", __name__, url_prefix="/discounts")


def _get_cities():
    return City.query.order_by(City.sort_order.asc(), City.name.asc()).all()


def _collect_city_statuses(form):
    """Collect every field named city_status_<id> into {city_id: value}."""
    statuses = {}
    for key, value in form.items():
        if key.startswith("city_status_"):
            try:
                statuses[int(key[len("city_status_"):])] = value
            except ValueError:
                continue
    return statuses


def _values_from_form(form):
    """Re-fill the form after a validation error."""
    return {
        "name": form.get("name", ""),
        "discount_type": form.get("discount_type", "percentage"),
        "value": form.get("value", ""),
        "aliases": form.get("aliases", ""),
        "terms": form.get("terms", ""),
        "is_active": form.get("is_active") == "on",
        "scope": form.get("scope", "all"),
        "city_status": _collect_city_statuses(form),
    }


def _values_from_discount(d):
    city_status = {l.city_id: ("active" if l.is_active else "inactive") for l in d.city_links}
    return {
        "name": d.name,
        "discount_type": d.discount_type,
        "value": f"{d.value:g}",
        "aliases": d.aliases or "",
        "terms": d.terms or "",
        "is_active": d.is_active,
        "scope": "specific" if city_status else "all",
        "city_status": city_status,
    }


def _empty_values():
    return {
        "name": "", "discount_type": "percentage", "value": "", "aliases": "",
        "terms": "", "is_active": True, "scope": "all", "city_status": {},
    }


@discount_bp.route("/", methods=["GET"])
def list_discounts():
    discounts = DiscountService.get_all_discounts()
    return render_template("discounts/list.html", discounts=discounts)


@discount_bp.route("/add", methods=["GET", "POST"])
def add_discount():
    cities = _get_cities()

    if request.method == "POST":
        service = DiscountService()
        discount, msg = service.create_discount(
            name=request.form.get("name"),
            discount_type=request.form.get("discount_type"),
            value=request.form.get("value"),
            aliases=request.form.get("aliases"),
            terms=request.form.get("terms"),
            is_active=request.form.get("is_active") == "on",
            scope=request.form.get("scope", "all"),
            city_statuses=_collect_city_statuses(request.form),
        )

        if discount:
            flash(msg, "success")
            return redirect(url_for("discount.list_discounts"))

        flash(msg, "danger")
        # Send the typed values back so they are not lost on error
        return render_template("discounts/add.html", v=_values_from_form(request.form), cities=cities)

    return render_template("discounts/add.html", v=_empty_values(), cities=cities)


@discount_bp.route("/<int:discount_id>", methods=["GET", "POST"])
def discount_detail(discount_id):
    service = DiscountService(discount_id=discount_id)
    discount, msg = service.get_discount()

    if not discount:
        flash(msg, "danger")
        return redirect(url_for("discount.list_discounts"))

    cities = _get_cities()

    if request.method == "POST":
        updated, update_msg = service.update_discount(
            name=request.form.get("name"),
            discount_type=request.form.get("discount_type"),
            value=request.form.get("value"),
            aliases=request.form.get("aliases"),
            terms=request.form.get("terms"),
            is_active=request.form.get("is_active") == "on",
            scope=request.form.get("scope", "all"),
            city_statuses=_collect_city_statuses(request.form),
        )
        if updated:
            flash(update_msg, "success")
            return redirect(url_for("discount.list_discounts"))

        flash(update_msg, "danger")
        return render_template(
            "discounts/detail.html",
            discount=discount, v=_values_from_form(request.form), cities=cities,
        )

    return render_template(
        "discounts/detail.html",
        discount=discount, v=_values_from_discount(discount), cities=cities,
    )


@discount_bp.route("/<int:discount_id>/toggle", methods=["POST"])
def toggle_discount(discount_id):
    service = DiscountService(discount_id=discount_id)
    discount, msg = service.toggle_active()
    flash(msg, "success" if discount else "danger")
    return redirect(url_for("discount.list_discounts"))


@discount_bp.route("/<int:discount_id>/delete", methods=["POST"])
def delete_discount(discount_id):
    service = DiscountService(discount_id=discount_id)
    deleted, msg = service.delete_discount()
    flash(msg, "success" if deleted else "danger")
    return redirect(url_for("discount.list_discounts"))