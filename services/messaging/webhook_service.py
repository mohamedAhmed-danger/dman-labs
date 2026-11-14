import logging
import time
from typing import Optional

from models.models import db
from platforms.registry import get_handler
from platforms.facebook.handler import FacebookHandler
from platforms.facebook.parser import parse_facebook_message, parse_facebook_comment
from platforms.waha.handler import WahaHandler
from platforms.waha.parser import parse_waha_message
from schemas.incoming_message import IncomingMessage
from services.shared.subscription_service import SubscriptionService
from services.shared.subscription_consumer import consume_subscription
from services.domain.page_service import PageService
from services.messaging.client_service import ClientService
from services.messaging.message_processor import run_agent
from services.messaging.redis_queue import get_redis_client, enqueue_message
from notification_center import send_production_alert
from config import Config


logger = logging.getLogger(__name__)


# ── Canonical Platform Lookup ─────────────────────────────────────────────────

def get_platform_id_for_facebook() -> int:
    """platform_id معرّف في FacebookHandler (ولازم يطابق id المنصة في جدول platforms)."""
    return FacebookHandler.platform_id


def get_platform_id_for_whatsapp() -> int:
    """platform_id معرّف في WahaHandler (ولازم يطابق id المنصة في جدول platforms)."""
    return WahaHandler.platform_id


# ── Feedback Helper ───────────────────────────────────────────────────────────

def build_post_booking_feedback_message(reference_id: Optional[str]) -> str:
    """Post-booking feedback message with a link to reference_id, or a generic version without one."""
    base_url = (Config.FEEDBACK_BASE_URL or "https://elbedawy-agent.revyai.tech").rstrip("/")
    if reference_id:
        url = f"{base_url}/feedback/{reference_id}"
        return f"يسعدنا تقييم تجربتك معنا من خلال الرابط التالي:\n{url}"
    return "يسعدنا تقييم تجربتك معنا ومشاركتنا رأيك لتحسين خدماتنا دائماً."


# ── Shared Helpers (used by both immediate replies and debounced agent replies) ──

def _fetch_page_and_check_quota(page_id: str, platform_id: int, log_prefix: str):
    """
    Look up the page for (page_id, platform_id) and verify its lab still has AI quota.
    Returns the page on success, or None (already logged) if the page is missing or quota is exhausted.
    """
    page = PageService.get_page_by_page_and_platform(page_id=page_id, platform_id=platform_id)
    if not page:
        logger.warning("[%s] Page not found: platform=%s page_id=%s", log_prefix, platform_id, page_id)
        return None

    allowed, reason = SubscriptionService.can_use_ai(page.laboratory_id)
    if not allowed:
        logger.warning(
            "[%s] Quota exhausted for lab=%s: %s. Dropping message silently.",
            log_prefix, page.laboratory_id, reason,
        )
        return None

    return page


def _send_reply(handler, user_id: str, reply: Optional[str], booking_pdf, visit_reference: Optional[str] = None):
    """Send the reply text, the booking PDF (if any), and the post-booking feedback message (if a PDF was sent)."""
    if reply:
        handler.send(user_id, reply)

    if booking_pdf:
        handler.send_image(recipient_id=user_id, file_bytes=booking_pdf, filename="booking_ticket.png")
        handler.send(user_id, build_post_booking_feedback_message(visit_reference))


# ── Message Dispatcher ────────────────────────────────────────────────────────

def dispatch_incoming_message(
    message: IncomingMessage,
    page_id: str,
    platform_id: int,
    laboratory_id: int,
    app,
):
    """
    Handle one incoming message:
    - immediate mode: reply right away
    - agent_text mode: queue it so the AI replies once the user stops typing (Redis debounced)
    """
    with app.app_context():
        # DESIGN DECISION: There is intentionally no lock wrapping dispatch_incoming_message or enqueue.
        # Two near-simultaneous messages landing on different workers is an accepted rare edge case
        # (at most a rare duplicate-billing event), traded off for simplicity.
        try:
            page = _fetch_page_and_check_quota(page_id, platform_id, "DISPATCH")
            if not page:
                return

            handler = get_handler(platform_id, page)
            client_service = ClientService(
                platform_id=platform_id,
                page_id=page_id,
                sender_id=message.sender_id,
            )
            client = client_service.get_or_create_client()
            
            if not client.city_id:
                handler.send_typing(message.sender_id)

                reply, pdf_bytes, visit_reference = run_agent(
                    message,
                    laboratory_id=laboratory_id,
                )

                if reply:
                    _send_reply(
                        handler,
                        message.sender_id,
                        reply,
                        pdf_bytes,
                        visit_reference,
                    )

                    # Deduct one message from subscription
                    consume_subscription(
                        message=message,
                        count=1,
                    )

                return


            result = handler.prepare(message)
            if not result:
                return

            if result.mode == "immediate":
                handler.send_typing(message.sender_id)
                _send_reply(handler, message.sender_id, result.text, result.extra)

            elif result.mode == "agent_text":
                handler.send_typing(message.sender_id)
                r = get_redis_client()
                enqueue_message(
                    r,
                    platform_id=platform_id,
                    page_id=page_id,
                    sender_id=message.sender_id,
                    platform_name=handler.platform_name,
                    text=result.text,
                    received_at=getattr(message, "received_at", None),
                    image_result=result.image_result,
                    sender_name=getattr(message, "sender_name", None),
                    sender_phone=getattr(message, "sender_phone", None),
                )

        except Exception as e:
            logger.exception("[DISPATCH] Error handling message from user: %s", e)
            send_production_alert(
                subject="Webhook Message Dispatch Error",
                body_or_error=e,
                context={
                    "page_id": page_id,
                    "platform_id": platform_id,
                    "laboratory_id": laboratory_id,
                    "error": str(e),
                },
            )
        finally:
            db.session.remove()


# ── Platform Webhook Processors ───────────────────────────────────────────────

def _process_facebook_entry(entry: dict, fb_platform_id: int, app):
    """Process one Facebook entry (one Page): its messaging events and its feed changes."""
    page_id = str(entry.get("id", "")).strip()
    if not page_id:
        return

    page = PageService.get_page_by_page_and_platform(page_id=page_id, platform_id=fb_platform_id)
    if not page:
        logger.warning("[FB WEBHOOK] Unknown page_id=%s for platform_id=%s", page_id, fb_platform_id)
        return

    # Read plain values once: dispatch_incoming_message calls db.session.remove(),
    # which can detach `page` on some Flask-SQLAlchemy versions.
    page_id_str = page.page_id
    laboratory_id = page.laboratory_id
    handler = get_handler(fb_platform_id, page)
    platform_name = handler.platform_name

    # 1. Direct user messaging events
    for messaging in entry.get("messaging", []):
        message_id = messaging.get("message", {}).get("mid")
        if message_id and not _claim_facebook_message(page_id_str, message_id):
            logger.info(
                "[FB WEBHOOK] Skipping duplicate message | page_id=%s | mid=%s",
                page_id_str, message_id,
            )
            continue

        messages = parse_facebook_message(
            messaging=messaging,
            page_id=page_id_str,
            platform_id=fb_platform_id,
            platform_name=platform_name,
        )
        for msg in messages:
            dispatch_incoming_message(
                message=msg,
                page_id=page_id_str,
                platform_id=fb_platform_id,
                laboratory_id=laboratory_id,
                app=app,
            )

    # 2. Page feed changes (Facebook Comments)
    for change in entry.get("changes", []):
        value = change.get("value") or {}
        sender = value.get("from") or {}
        logger.info(
            "[FB COMMENT] change received | page=%s field=%s item=%s verb=%s "
            "comment_id=%s parent_id=%s post_id=%s from=%s",
            page_id_str, change.get("field"), value.get("item"), value.get("verb"),
            value.get("comment_id"), value.get("parent_id"), value.get("post_id"),
            sender.get("id"),
        )

        try:
            comment_info = parse_facebook_comment(change)
        except Exception:
            logger.exception("[FB COMMENT] parser crashed | page=%s", page_id_str)
            continue

        if not comment_info:
            logger.info(
                "[FB COMMENT] SKIPPED by parser | field=%s item=%s verb=%s",
                change.get("field"), value.get("item"), value.get("verb"),
            )
            continue

        comment_id = comment_info.get("comment_id")
        if not comment_id:
            logger.warning("[FB COMMENT] SKIPPED: parser returned no comment_id | info=%s", comment_info)
            continue

        # الصفحة بترد على نفسها: نتخطى
        if str(sender.get("id", "")) == page_id_str:
            logger.info("[FB COMMENT] SKIPPED: comment from the page itself | comment_id=%s", comment_id)
            continue

        # نفس التعليق ممكن يوصل أكتر من مرة (retry من فيسبوك)
        if not _claim_facebook_comment(page_id_str, comment_id):
            logger.info("[FB COMMENT] SKIPPED: duplicate | comment_id=%s", comment_id)
            continue

        target_page_id = comment_info.get("page_id") or page_id_str
        logger.info("[FB COMMENT] Processing | comment_id=%s target_page=%s", comment_id, target_page_id)
        try:
            handler.handle_comment(comment_id=comment_id, page_id=target_page_id)
        except Exception as e:
            logger.exception("[FB COMMENT] handle_comment crashed | comment_id=%s", comment_id)
            send_production_alert(
                subject="Facebook Comment Handler Exception",
                body_or_error=e,
                context={"comment_id": comment_id, "page_id": page_id_str},
            )


def _claim_facebook_message(page_id: str, message_id: str) -> bool:
    """Atomically accept a Facebook message ID once, including across webhook retries."""
    try:
        redis_client = get_redis_client()
        return bool(
            redis_client.set(
                f"facebook:webhook:message:{page_id}:{message_id}",
                "1",
                nx=True,
                ex=24 * 60 * 60,
            )
        )
    except Exception:
        logger.exception(
            "[FB WEBHOOK] Could not check duplicate message | page_id=%s | mid=%s",
            page_id, message_id,
        )
        return True


def _claim_facebook_comment(page_id: str, comment_id: str) -> bool:
    """يقبل كل comment_id مرة واحدة بس (حماية من retries فيسبوك)."""
    try:
        redis_client = get_redis_client()
        return bool(
            redis_client.set(
                f"facebook:webhook:comment:{page_id}:{comment_id}",
                "1",
                nx=True,
                ex=24 * 60 * 60,
            )
        )
    except Exception:
        logger.exception("[FB COMMENT] Could not check duplicate | comment_id=%s", comment_id)
        return True


def process_facebook_payload(payload: dict, app):
    """Background worker for Facebook webhook events: isolates each entry by Page ID, routes messages and comments."""
    with app.app_context():
        entries = payload.get("entry", [])
        try:
            fb_platform_id = get_platform_id_for_facebook()

            for entry in entries:
                # A failure in one page must not drop the remaining entries in the batch
                try:
                    _process_facebook_entry(entry, fb_platform_id, app)
                except Exception as e:
                    entry_id = entry.get("id") if isinstance(entry, dict) else None
                    logger.exception("[FB WEBHOOK] Entry processing error page_id=%s: %s", entry_id, e)
                    send_production_alert(
                        subject="Facebook Webhook Entry Exception",
                        body_or_error=e,
                        context={
                            "platform": "Facebook",
                            "page_id": entry_id,
                            "error": str(e),
                        },
                    )

        except Exception as e:
            logger.exception("[FB WEBHOOK] Worker processing error: %s", e)
            send_production_alert(
                subject="Facebook Webhook Worker Exception",
                body_or_error=e,
                context={
                    "platform": "Facebook",
                    "entries_count": len(entries),
                    "error": str(e),
                },
            )
        finally:
            db.session.remove()


def process_waha_payload(payload_data: dict, app):
    """
    Background worker for WAHA WhatsApp events. Resolves the receiving tenant strictly via me.lid/me.id
    with exact matching only (no fallback, no fuzzy matching) to keep tenants isolated.
    """
    with app.app_context():
        try:
            me_info = payload_data.get("me") or {}
            receiving_lid = me_info.get("lid") or me_info.get("id")

            if not receiving_lid:
                logger.warning("[WAHA WEBHOOK] Payload received without me.lid or me.id. Dropping message for tenant safety.")
                return

            whatsapp_platform_id = get_platform_id_for_whatsapp()
            receiving_lid_str = str(receiving_lid).strip()
            clean_lid = receiving_lid_str.split("@")[0]

            # Exact matching only: no fuzzy matching, no wildcard LIKE, no arbitrary fallback
            page = PageService.get_page_by_page_and_platform(
                page_id=receiving_lid_str,
                platform_id=whatsapp_platform_id,
            )
            if not page and clean_lid != receiving_lid_str:
                page = PageService.get_page_by_page_and_platform(
                    page_id=clean_lid,
                    platform_id=whatsapp_platform_id,
                )

            if not page:
                logger.warning(
                    "[WAHA WEBHOOK] Unknown receiving_lid=%s. No matching Page registered for WhatsApp platform=%s. "
                    "Halting message processing to ensure strict tenant isolation.",
                    receiving_lid_str, whatsapp_platform_id,
                )
                return

            page_id_str = page.page_id
            laboratory_id = page.laboratory_id
            handler = get_handler(whatsapp_platform_id, page)

            inner_payload = payload_data.get("payload", {})
            if not inner_payload:
                return

            msg = parse_waha_message(
                payload=inner_payload,
                page_id=page_id_str,
                platform_id=whatsapp_platform_id,
                platform_name=handler.platform_name,
            )

            if msg:
                dispatch_incoming_message(
                    message=msg,
                    page_id=page_id_str,
                    platform_id=whatsapp_platform_id,
                    laboratory_id=laboratory_id,
                    app=app,
                )

        except Exception as e:
            logger.exception("[WAHA WEBHOOK] Worker processing error: %s", e)
            send_production_alert(
                subject="WAHA Webhook Worker Exception",
                body_or_error=e,
                context={
                    "platform": "WhatsApp",
                    "event": payload_data.get("event"),
                    "has_payload": bool(payload_data.get("payload")),
                    "error": str(e),
                },
            )
        finally:
            db.session.remove()