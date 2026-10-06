from flask import Blueprint, request, jsonify, send_file
from flask_login import login_required
from services.domain.price_import_service import (
    preview_import,
    commit_import,
    get_import_progress,
    retry_pending,
)
from services.domain.price_export_service import export_prices_excel

price_bp = Blueprint("price", __name__, url_prefix="/prices")


@price_bp.route("/import/preview", methods=["POST"])
@login_required
def import_preview():
    if "file" not in request.files:
        return jsonify({"error": "يرجى رفع ملف الأسعار"}), 400

    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"error": "اسم الملف غير صحيح"}), 400

    try:
        city_id = request.form.get("city_id", type=int)
        laboratory_id = request.form.get("laboratory_id", type=int) or 1
        delete_missing = request.form.get("delete_missing", "false").lower() in ("true", "1", "yes")

        if not city_id:
            return jsonify({"error": "يرجى اختيار المنطقة"}), 400

        result = preview_import(
            file=file,
            laboratory_id=laboratory_id,
            city_id=city_id,
            delete_missing=delete_missing
        )
        if "error" in result:
            return jsonify({"error": result["error"]}), 400

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"حدث خطأ أثناء معاينة الملف: {e}"}), 500


@price_bp.route("/import/commit", methods=["POST"])
@login_required
def import_commit():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    city_id = data.get("city_id")
    laboratory_id = data.get("laboratory_id", 1)
    delete_missing = bool(data.get("delete_missing", False))
    generate_ai = bool(data.get("generate_ai", True))

    if not token or not city_id:
        return jsonify({"error": "بيانات التأكيد غير مكتملة"}), 400

    try:
        result = commit_import(
            token=token,
            laboratory_id=int(laboratory_id),
            city_id=int(city_id),
            delete_missing=delete_missing,
            generate_ai=generate_ai
        )
        if "error" in result:
            return jsonify({"error": result["error"]}), 400

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"حدث خطأ أثناء تنفيذ الاستيراد: {e}"}), 500


@price_bp.route("/import/progress/<token>", methods=["GET"])
@login_required
def import_progress(token):
    progress = get_import_progress(token)
    if not progress:
        return jsonify({"error": "رمز المتابعة غير موجود"}), 404
    return jsonify(progress)


@price_bp.route("/export", methods=["GET"])
@login_required
def export_prices():
    city_id = request.args.get("city_id", type=int)
    laboratory_id = request.args.get("laboratory_id", type=int) or 1

    if not city_id:
        return jsonify({"error": "يرجى اختيار المنطقة للتصدير"}), 400

    excel_io, filename = export_prices_excel(laboratory_id=laboratory_id, city_id=city_id)
    if not excel_io:
        return jsonify({"error": "تعذر إنشاء ملف التصدير"}), 500

    return send_file(
        excel_io,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )


@price_bp.route("/retry_pending", methods=["POST"])
@login_required
def retry_pending_route():
    data = request.get_json(silent=True) if request.is_json else request.form
    city_id = data.get("city_id", type=int) if not request.is_json else data.get("city_id")
    laboratory_id = data.get("laboratory_id", 1)

    if not city_id:
        return jsonify({"error": "يرجى اختيار المنطقة"}), 400

    result = retry_pending(laboratory_id=int(laboratory_id), city_id=int(city_id))
    return jsonify(result)
