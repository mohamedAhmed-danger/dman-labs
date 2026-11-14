import logging

from flask import Blueprint, render_template, request, flash, redirect, url_for
from flask_login import login_required, current_user

from services.shared.subscription_service import SubscriptionService, MAX_RENEW_MONTHS
from services.domain.laboratory_service import LaboratoryService

logger = logging.getLogger(__name__)

subscription_bp = Blueprint('subscription', __name__, url_prefix='/admin/subscription')

DASHBOARD = "subscription.admin_subscription"


def _get_service():
    """Return a SubscriptionService only if the current lab has a subscription."""
    lab_id = LaboratoryService().get_current_laboratory_id()
    if not lab_id:
        return None
    service = SubscriptionService(laboratory_id=lab_id)
    return service if service.subscription else None


def _parse_int(field: str, default: int, minimum: int, maximum: int = None):
    """Read an int from the form; return None if invalid or out of range."""
    try:
        value = int(request.form.get(field, default))
    except (TypeError, ValueError):
        return None
    if value < minimum or (maximum is not None and value > maximum):
        return None
    return value


def _redirect_back():
    """ارجع للصفحة اللي اتبعت منها الفورم (روابط داخلية بس)."""
    target = request.form.get("next", "")
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return redirect(target)
    return redirect(url_for(DASHBOARD))


@subscription_bp.route("/dashboard")
@login_required
def admin_subscription():
    service = _get_service()
    if not service:
        flash("لا توجد بيانات اشتراك.", "error")
        return redirect(url_for("main.dashboard"))

    return render_template(
        "subscription/index.html",
        subscription=service.subscription,
        status=service.get_status(),
        alert=service.get_alert(),
        remaining=service.messages_remaining(),
        usage=service.usage_percentage(),
    )


@subscription_bp.route("/renew", methods=["POST"])
@login_required
def renew_subscription():
    service = _get_service()
    if not service:
        flash("لا توجد بيانات اشتراك.", "error")
        return redirect(url_for("main.dashboard"))

    months = _parse_int("months", 1, 1, MAX_RENEW_MONTHS)
    if months is None:
        flash(f"عدد الشهور غير صحيح (من 1 إلى {MAX_RENEW_MONTHS}).", "error")
    elif service.renew(months=months):
        logger.info("[AUDIT] user=%s renewed lab=%s months=%d",
                    current_user.id, service.laboratory_id, months)
        flash("تم تجديد الاشتراك بنجاح.", "success")
    else:
        flash("فشل تجديد الاشتراك.", "error")
    return redirect(url_for(DASHBOARD))


@subscription_bp.route("/reset", methods=["POST"])
@login_required
def reset_subscription_usage():
    service = _get_service()
    if not service:
        flash("لا توجد بيانات اشتراك.", "error")
        return redirect(url_for("main.dashboard"))

    if service.reset_usage():
        logger.info("[AUDIT] user=%s reset usage lab=%s", current_user.id, service.laboratory_id)
        if service._is_expired(service.subscription):
            flash("تم تصفير الاستهلاك، لكن الاشتراك منتهي بالتاريخ. استخدم التجديد.", "warning")
        else:
            flash("تم إعادة ضبط الاستهلاك بنجاح.", "success")
    else:
        flash("فشل إعادة ضبط الاستهلاك.", "error")
    return redirect(url_for(DASHBOARD))


@subscription_bp.route("/suspend", methods=["POST"])
@login_required
def suspend_subscription():
    service = _get_service()
    if not service:
        flash("لا توجد بيانات اشتراك.", "error")
        return redirect(url_for("main.dashboard"))

    if service.suspend():
        logger.info("[AUDIT] user=%s suspended lab=%s", current_user.id, service.laboratory_id)
        flash("تم إيقاف الاشتراك.", "warning")
    else:
        flash("فشل إيقاف الاشتراك.", "error")
    return _redirect_back()


@subscription_bp.route("/activate", methods=["POST"])
@login_required
def activate_subscription():
    service = _get_service()
    if not service:
        flash("لا توجد بيانات اشتراك.", "error")
        return redirect(url_for("main.dashboard"))

    if service.activate():
        logger.info("[AUDIT] user=%s activated lab=%s", current_user.id, service.laboratory_id)
        flash("تم تنشيط الاشتراك.", "success")
    else:
        flash("فشل تنشيط الاشتراك.", "error")
    return _redirect_back()


@subscription_bp.route("/update-limit", methods=["POST"])
@login_required
def update_subscription_limit():
    service = _get_service()
    if not service:
        flash("لا توجد بيانات اشتراك.", "error")
        return redirect(url_for("main.dashboard"))

    new_limit = _parse_int("new_limit", 0, 0)
    if new_limit is None:
        flash("قيمة حد الرسائل غير صحيحة.", "error")
    elif service.update_limit(message_limit=new_limit):
        logger.info("[AUDIT] user=%s set message_limit=%d lab=%s",
                    current_user.id, new_limit, service.laboratory_id)
        flash("تم تحديث حد الرسائل بنجاح.", "success")
    else:
        flash("فشل تحديث حد الرسائل.", "error")
    return redirect(url_for(DASHBOARD))


@subscription_bp.route("/update-grace", methods=["POST"])
@login_required
def update_subscription_grace():
    service = _get_service()
    if not service:
        flash("لا توجد بيانات اشتراك.", "error")
        return redirect(url_for("main.dashboard"))

    new_grace = _parse_int("new_grace", 0, 0)
    if new_grace is None:
        flash("قيمة فترة السماح غير صحيحة.", "error")
    elif service.update_grace_limit(grace_limit=new_grace):
        logger.info("[AUDIT] user=%s set grace_limit=%d lab=%s",
                    current_user.id, new_grace, service.laboratory_id)
        flash("تم تحديث فترة السماح بنجاح.", "success")
    else:
        flash("فشل تحديث فترة السماح.", "error")
    return redirect(url_for(DASHBOARD))