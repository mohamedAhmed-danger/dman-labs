import logging

from services.domain.page_service import PageService
from notification_center import send_production_alert
from services.shared.subscription_service import SubscriptionService

logger = logging.getLogger(__name__)


OCR_MESSAGE_COST = 2
DEFAULT_MESSAGE_COST = 1


def consume_subscription(message, usage: dict = None, count: int = None) -> bool:
    """Deduct messages and cost from the laboratory subscription. Returns True on success."""
    try:
        page = PageService.get_page_by_page_and_platform(
            page_id=message.page_id,
            platform_id=message.platform_id,
        )
        if not page:
            logger.warning("[consume_subscription] No page for page_id=%s platform_id=%s",
                           message.page_id, message.platform_id)
            return False

        subscription = SubscriptionService.get_by_page(page)
        if not subscription:
            logger.warning("[consume_subscription] No subscription for laboratory_id=%s",
                           page.laboratory_id)
            return False

        if count is not None:
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                logger.error("[consume_subscription] Invalid count=%r", count)
                return False
            messages_to_deduct = count
        else:
            breakdown = (usage or {}).get("breakdown") or {}
            used_ocr = "ocr_vision_usage" in breakdown
            messages_to_deduct = OCR_MESSAGE_COST if used_ocr else DEFAULT_MESSAGE_COST

        cost = (usage or {}).get("total_cost_usd") or 0.0
        lab_id = getattr(subscription, "laboratory_id", None)

        if not SubscriptionService.consume(subscription, count=messages_to_deduct, cost=cost):
            logger.error("[BILLING] Deduction FAILED | lab_id=%s | units=%d", lab_id, messages_to_deduct)
            return False

        logger.info(
            "[BILLING] Deducted %d unit(s) | type=%s | sender_id=%s | lab_id=%s",
            messages_to_deduct,
            getattr(message, "type", None) or getattr(message, "msg_type", "text"),
            getattr(message, "sender_id", "unknown"),
            lab_id,
        )
        return True

    except Exception as e:
        logger.exception("[consume_subscription] Error deducting messages: %s", e)
        try:
            send_production_alert(
                subject="Subscription Consumption Failure",
                body_or_error=e,
                context={
                    "page_id": getattr(message, "page_id", None),
                    "platform_id": getattr(message, "platform_id", None),
                    "sender_id": getattr(message, "sender_id", None),
                    "usage": usage,
                },
            )
        except Exception:
            logger.exception("[consume_subscription] Failed to send production alert")
        return False