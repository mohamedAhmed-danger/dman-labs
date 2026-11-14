import logging
from dataclasses import dataclass
from typing import Optional, Any

from langchain_core.tools import tool

from services.domain.booking_service import BookingService


logger = logging.getLogger(__name__)


@dataclass
class SaveVisitResult:
    success: bool
    visit: Any = None
    message: Optional[str] = None


@tool
def save_visit_tool(
    name: str,
    phone_number: str,
    address: str,
    details: str,
    date: str,
    time: str,
    comes_from: str = "WhatsApp",
    city_id: int | None = None,
    page_id: str | None = None,
    platform_id: int | None = None,
    sender_id: str | None = None,
) -> SaveVisitResult:
    """Saves a confirmed home visit medical laboratory booking."""

    try:
        if not city_id:
            return SaveVisitResult(
                success=False,
                message="A validated client region is required before saving a booking.",
            )

        # 1. Save booking
        booking_service = BookingService()
        identity = None
        try:
            from utils.contact_resolver import resolve_user_identity

            identity = resolve_user_identity(page_id, platform_id, sender_id, fallback_name=name)
        except Exception:
            logger.exception("[VISIT TOOL] Could not resolve user identity")
            identity = name or sender_id

        result = booking_service.save_booking(
            name=name,
            phone=phone_number,
            date=date,
            details=details,
            comes_from=comes_from,
            time=time,
            address=address,
            city_id=city_id,
            user_identity=identity,
        )

        # 2. Make sure booking was saved successfully
        if not result or not result.reference_id:
            return SaveVisitResult(
                success=False,
                message="Failed to generate booking reference.",
            )

        # 3. Return booking result
        return SaveVisitResult(
            success=True,
            visit=result,
            message="Booking saved successfully.",
        )

    except Exception as e:
        logger.exception(
            "Error inside save_visit_tool"
        )

        return SaveVisitResult(
            success=False,
            message=str(e),
        )