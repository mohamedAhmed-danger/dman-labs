from flask import Blueprint, render_template, request, redirect, url_for, flash
from models.models import City
from services.domain.bundle_service import BundleService

bundle_bp = Blueprint("bundle", __name__, url_prefix="/bundles")


def _get_cities():
    return City.query.order_by(City.sort_order.asc(), City.name.asc()).all()


def _collect_city_prices(form):
    """Collect every field named city_price_<id> into {city_id: value}."""
    prices = {}
    for key, value in form.items():
        if key.startswith("city_price_"):
            try:
                prices[int(key[len("city_price_"):])] = value
            except ValueError:
                continue
    return prices


@bundle_bp.route("/", methods=["GET"])
def list_bundles():
    bundles = BundleService.get_all_bundles()
    return render_template("bundles/list.html", bundles=bundles)


@bundle_bp.route("/add", methods=["GET", "POST"])
def add_bundle():
    cities = _get_cities()

    if request.method == "POST":
        service = BundleService()
        bundle, msg = service.create_bundle(
            name=request.form.get("name"),
            price=request.form.get("price"),
            description=request.form.get("description"),
            instructions=request.form.get("instructions"),
            city_prices=_collect_city_prices(request.form),
        )

        if bundle:
            flash(msg, "success")
            return redirect(url_for("bundle.list_bundles"))

        flash(msg, "danger")
        # Send the typed values back so they are not lost on error
        return render_template("bundles/add.html", form=request.form, cities=cities)

    return render_template("bundles/add.html", form={}, cities=cities)


def _render_detail(bundle):
    city_prices = {cp.city_id: cp.price for cp in bundle.city_prices}
    return render_template(
        "bundles/detail.html",
        bundle=bundle,
        cities=_get_cities(),
        city_prices=city_prices,
    )


@bundle_bp.route("/<int:bundle_id>", methods=["GET", "POST"])
def bundle_detail(bundle_id):
    service = BundleService(bundle_id=bundle_id)
    bundle, msg = service.get_bundle()

    if not bundle:
        flash(msg, "danger")
        return redirect(url_for("bundle.list_bundles"))

    if request.method == "POST":
        updated_bundle, update_msg = service.update_bundle(
            name=request.form.get("name"),
            price=request.form.get("price"),
            description=request.form.get("description"),
            instructions=request.form.get("instructions"),
        )
        if updated_bundle:
            flash(update_msg, "success")
            return redirect(url_for("bundle.list_bundles"))

        flash(update_msg, "danger")

    return _render_detail(bundle)


@bundle_bp.route("/<int:bundle_id>/city-prices", methods=["POST"])
def save_city_prices(bundle_id):
    service = BundleService(bundle_id=bundle_id)
    bundle, msg = service.set_city_prices(_collect_city_prices(request.form))
    flash(msg, "success" if bundle else "danger")
    return redirect(url_for("bundle.bundle_detail", bundle_id=bundle_id))


@bundle_bp.route("/<int:bundle_id>/delete", methods=["POST"])
def delete_bundle(bundle_id):
    service = BundleService(bundle_id=bundle_id)
    deleted, msg = service.delete_bundle()
    flash(msg, "success" if deleted else "danger")
    return redirect(url_for("bundle.list_bundles"))