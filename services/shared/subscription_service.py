import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from models.models import Subscription, db

logger = logging.getLogger(__name__)

MAX_RENEW_MONTHS = 36
LOW_QUOTA_THRESHOLD = 500
DAYS_PER_MONTH = 30


def _is_int(value) -> bool:
    """True for real ints only (bool is rejected)."""
    return isinstance(value, int) and not isinstance(value, bool)


class SubscriptionService:
    def __init__(self, laboratory_id=None, subscription=None):
        self.laboratory_id = laboratory_id
        self._subscription = subscription
        if subscription and getattr(subscription, "laboratory_id", None):
            self.laboratory_id = subscription.laboratory_id

    @property
    def subscription(self):
        if self._subscription is None and self.laboratory_id is not None:
            self._subscription = Subscription.query.filter_by(
                laboratory_id=self.laboratory_id
            ).first()
        return self._subscription

    def _resolve_subscription(self, subscription=None):
        return subscription if subscription is not None else self.subscription

    # ==========================================================
    # Small helpers (None-safe values + expiry check in ONE place)
    # ==========================================================

    @staticmethod
    def _used(sub) -> int:
        return sub.message_used or 0

    @staticmethod
    def _limit(sub) -> int:
        return sub.message_limit or 0

    @staticmethod
    def _grace(sub) -> int:
        return sub.grace_limit or 0

    @staticmethod
    def _is_expired(sub) -> bool:
        if not sub.end_date:
            return False
        end = sub.end_date if sub.end_date.tzinfo else sub.end_date.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > end

    def _apply(self, subscription, label: str, **fields) -> bool:
        """Set fields on the subscription, commit, return True/False."""
        sub = self._resolve_subscription(subscription)
        if not sub:
            logger.warning("[SubscriptionService.%s] No subscription found.", label)
            return False
        try:
            for key, value in fields.items():
                setattr(sub, key, value)
            sub.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            self._subscription = sub
            return True
        except Exception:
            db.session.rollback()
            logger.exception("[SubscriptionService.%s] Error", label)
            return False

    # ==========================================================
    # Getters
    # ==========================================================

    @staticmethod
    def get_subscription_by_laboratory_id(laboratory_id):
        if not laboratory_id:
            return None
        try:
            return Subscription.query.filter_by(laboratory_id=laboratory_id).first()
        except Exception:
            logger.exception("[SubscriptionService.get_subscription_by_laboratory_id] Error")
            return None

    @staticmethod
    def get_by_page(page):
        if not page or not getattr(page, "laboratory_id", None):
            return None
        return SubscriptionService.get_subscription_by_laboratory_id(page.laboratory_id)

    def messages_remaining(self, subscription=None) -> int:
        sub = self._resolve_subscription(subscription)
        if not sub:
            return 0
        return max(self._limit(sub) - self._used(sub), 0)

    def grace_remaining(self, subscription=None) -> int:
        sub = self._resolve_subscription(subscription)
        if not sub:
            return 0
        extra = max(self._used(sub) - self._limit(sub), 0)
        return max(self._grace(sub) - extra, 0)

    def usage_percentage(self, subscription=None):
        sub = self._resolve_subscription(subscription)
        if not sub or self._limit(sub) == 0:
            return 0
        return round((self._used(sub) / self._limit(sub)) * 100, 1)

    # ==========================================================
    # AI Permission
    # ==========================================================

    @staticmethod
    def can_use_ai(target=None, subscription=None) -> tuple[bool, str]:
        """
        Call shapes:
            SubscriptionService.can_use_ai(service_instance)
            SubscriptionService.can_use_ai(subscription_model)
            SubscriptionService.can_use_ai(laboratory_id_int)
            SubscriptionService.can_use_ai(subscription=sub)
        """
        sub = None
        if isinstance(target, SubscriptionService):
            sub = target._resolve_subscription(subscription)
        elif isinstance(target, Subscription):
            sub = target
        elif _is_int(target):
            sub = SubscriptionService.get_subscription_by_laboratory_id(target)
        elif hasattr(target, "laboratory_id"):
            sub = SubscriptionService.get_subscription_by_laboratory_id(target.laboratory_id)
        elif subscription is not None:
            sub = subscription

        if not sub:
            return False, "No subscription."
        if not sub.is_active:
            return False, "Subscription suspended."
        if SubscriptionService._is_expired(sub):
            return False, "Subscription expired by date."

        total = SubscriptionService._limit(sub) + SubscriptionService._grace(sub)
        if SubscriptionService._used(sub) >= total:
            return False, "Message limit exceeded."
        return True, "OK"

    # ==========================================================
    # Consume (atomic + conditional)
    # ==========================================================

    @staticmethod
    def consume(subscription, count: int = 1, cost: float = None) -> bool:
        """
        Atomically add `count` to message_used (and `cost` to estimated_cost)
        in ONE UPDATE statement, only while used < limit + grace.

        - No lost updates: the DB does the increment, not Python.
        - Returns False if nothing was deducted (limit reached / error / bad input).
        - Note: the condition is `used < total`, so a deduction that STARTS under
          the ceiling is allowed to cross it slightly (e.g. an OCR message costing 2
          when only 1 is left). The reply was already served, so we record it.
        """
        if not subscription:
            logger.warning("[SubscriptionService.consume] No valid Subscription instance provided.")
            return False
        if not _is_int(count) or count <= 0:
            logger.error("[SubscriptionService.consume] Invalid count=%r", count)
            return False

        lab_id = getattr(subscription, "laboratory_id", None)
        try:
            now = datetime.now(timezone.utc)
            values = {
                Subscription.message_used: func.coalesce(Subscription.message_used, 0) + count,
                Subscription.updated_at: now,
            }
            if cost:
                values[Subscription.estimated_cost] = (
                    func.coalesce(Subscription.estimated_cost, 0.0) + float(cost)
                )

            total_allowed = (
                func.coalesce(Subscription.message_limit, 0)
                + func.coalesce(Subscription.grace_limit, 0)
            )
            rows = (
                Subscription.query
                .filter(
                    Subscription.id == subscription.id,
                    func.coalesce(Subscription.message_used, 0) < total_allowed,
                )
                .update(values, synchronize_session=False)
            )
            db.session.commit()

            if rows == 0:
                logger.warning(
                    "[SubscriptionService.consume] Limit reached, nothing deducted | lab_id=%s", lab_id
                )
                return False

            db.session.refresh(subscription)  # sync the in-memory object with the DB
            logger.info(
                "[SubscriptionService.consume] Deducted %d | cost=%s USD | lab_id=%s | total_used=%s",
                count, cost, lab_id, subscription.message_used,
            )
            return True

        except Exception as e:
            db.session.rollback()
            logger.exception("[SubscriptionService.consume] Error")
            try:
                from notification_center import send_production_alert
                send_production_alert(
                    subject="Subscription Deduction Database Failure",
                    body_or_error=e,
                    context={"laboratory_id": lab_id, "count": count, "cost": cost},
                    level="ERROR",
                )
            except Exception:
                logger.exception("[SubscriptionService.consume] Failed to send alert")
            return False

    # ==========================================================
    # Status / Alerts
    # ==========================================================

    def get_status(self, subscription=None):
        sub = self._resolve_subscription(subscription)
        if not sub:
            return {"text": "No Subscription", "color": "danger"}
        if not sub.is_active:
            return {"text": "Suspended", "color": "danger"}
        if self._is_expired(sub):
            return {"text": "Expired", "color": "danger"}
        if self._used(sub) >= self._limit(sub) + self._grace(sub):
            return {"text": "Limit Reached", "color": "warning"}
        return {"text": "Active", "color": "success"}

    def get_alert(self, subscription=None):
        sub = self._resolve_subscription(subscription)
        if not sub:
            return {"type": "danger", "message": "No subscription found."}
        if not sub.is_active:
            return {"type": "danger", "message": "AI service is suspended."}
        if self._is_expired(sub):
            return {"type": "danger", "message": "Subscription has expired by date."}

        remaining = self.messages_remaining(sub)
        if remaining == 0:
            grace = self.grace_remaining(sub)
            if grace > 0:
                return {"type": "warning",
                        "message": f"Main limit reached. {grace} grace messages remaining."}
            return {"type": "danger", "message": "Message limit exceeded."}

        if remaining <= LOW_QUOTA_THRESHOLD:
            return {"type": "warning", "message": f"Only {remaining} messages remaining."}
        return None

    # ==========================================================
    # Admin operations (all return True/False)
    # ==========================================================

    def renew(self, months: int = 1, subscription=None) -> bool:
        """
        Renew FROM NOW: start_date = now, end_date = now + 30*months days.
        Resets message_used and re-activates. Any remaining days of the old
        period are NOT carried over.
        """
        if not _is_int(months) or not 1 <= months <= MAX_RENEW_MONTHS:
            logger.error("[SubscriptionService.renew] Invalid months=%r", months)
            return False

        sub = self._resolve_subscription(subscription)
        if not sub:
            logger.warning("[SubscriptionService.renew] No subscription found.")
            return False

        now = datetime.now(timezone.utc)
        return self._apply(
            sub, "renew",
            message_used=0,
            is_active=True,
            start_date=now,
            end_date=now + timedelta(days=DAYS_PER_MONTH * months),
            renew_count=(sub.renew_count or 0) + 1,
            last_renewed_at=now,
        )

    def reset_usage(self, subscription=None) -> bool:
        """Reset message_used to 0 only. Dates are NOT touched (use renew for that)."""
        return self._apply(subscription, "reset_usage", message_used=0)

    def suspend(self, subscription=None) -> bool:
        return self._apply(subscription, "suspend", is_active=False)

    def activate(self, subscription=None) -> bool:
        return self._apply(subscription, "activate", is_active=True)

    def update_limit(self, message_limit: int, subscription=None) -> bool:
        if not _is_int(message_limit) or message_limit < 0:
            logger.error("[SubscriptionService.update_limit] Invalid limit=%r", message_limit)
            return False
        return self._apply(subscription, "update_limit", message_limit=message_limit)

    def update_grace_limit(self, grace_limit: int, subscription=None) -> bool:
        if not _is_int(grace_limit) or grace_limit < 0:
            logger.error("[SubscriptionService.update_grace_limit] Invalid grace=%r", grace_limit)
            return False
        return self._apply(subscription, "update_grace_limit", grace_limit=grace_limit)