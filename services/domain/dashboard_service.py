import logging

from models.models import db
from services.domain.laboratory_service import LaboratoryService
from services.messaging.client_service import ClientService
from services.domain.booking_service import BookingService
from services.domain.inquiry_service import InquiryService
from services.domain.tests_service import TestsService
from services.shared.subscription_service import SubscriptionService

logger = logging.getLogger(__name__)


def _safe(label: str, fn, default=0):
    """
    ينفّذ fn ويرجّع نتيجتها. لو فشلت: يسجّل الخطأ ويرجّع default،
    عشان فشل رقم واحد ميوقّعش الداشبورد كلها.
    """
    try:
        return fn()
    except Exception:
        db.session.rollback()  # بدونها أي استعلام بعد الخطأ ممكن يفشل هو كمان
        logger.exception("[DashboardService] %s failed", label)
        return default


class DashboardService:
    def __init__(self, laboratory_id=None):
        self.laboratory_id = laboratory_id

    def _resolve_laboratory_id(self):
        """الـ laboratory_id المتبعت، أو أول معمل في الداتابيز لو مفيش."""
        if self.laboratory_id:
            return self.laboratory_id
        laboratory = LaboratoryService.get_first_laboratory()
        return laboratory.id if laboratory else None

    def _get_subscription_data(self, lab_id) -> dict:
        sub_service = SubscriptionService(laboratory_id=lab_id)
        subscription = sub_service.subscription
        if not subscription:
            return {}
        return {
            "subscription": subscription,
            "subscription_status": sub_service.get_status(),
            "usage_percentage": sub_service.usage_percentage(),
            "remaining_messages": sub_service.messages_remaining(),
        }

    def get_summary_data(self) -> dict:
        """
        أرقام الداشبورد: حالة الاشتراك والاستهلاك وعدّادات العملاء والحجوزات والاستفسارات والتحاليل.
        كل رقم بيفشل لوحده ويرجع قيمته الافتراضية (0) مع تسجيل الخطأ.
        """
        lab_id = _safe("resolve laboratory", self._resolve_laboratory_id, default=None)

        data = {
            "subscription": None,
            "subscription_status": None,
            "usage_percentage": 0,
            "remaining_messages": 0,
            "clients_count": _safe("clients count", ClientService.get_total_clients_count),
            "booking_stats": {"total": _safe("bookings count", BookingService.get_total_bookings_count)},
            "pending_inquiries_count": _safe("pending inquiries count", InquiryService.get_pending_inquiries_count),
            "tests_count": _safe("tests count", TestsService.get_total_tests_count),
            "cities_breakdown": [],
        }

        if lab_id:
            data.update(_safe("subscription data", lambda: self._get_subscription_data(lab_id), default={}))
            def _get_cities_breakdown():
                from services.domain.city_service import CityService
                cities = CityService.get_cities_by_laboratory(lab_id)
                return [{"id": c.id, "name": c.name, "count": TestsService.get_total_tests_count(laboratory_id=lab_id, city_id=c.id)} for c in cities]
            data["cities_breakdown"] = _safe("cities breakdown", _get_cities_breakdown, default=[])

        return data