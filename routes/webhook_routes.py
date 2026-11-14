
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from flask import (
    Blueprint,
    request,
    abort,
    current_app,
)

from config import Config

from services.messaging.webhook_service import (
    process_facebook_payload,
    process_waha_payload,
)


logger = logging.getLogger(__name__)

webhook_bp = Blueprint(
    "webhook",
    __name__,
    url_prefix="/webhook",
)

VERIFY_TOKEN = (
    os.environ.get("VERIFY_TOKEN")
    or os.environ.get("FB_VERIFY_TOKEN")
    or ""
)

webhook_executor = ThreadPoolExecutor(
    max_workers=Config.WEBHOOK_THREAD_POOL_SIZE,
    thread_name_prefix="webhook_worker",
)


# ============================================================
# FACEBOOK WEBHOOK
# ============================================================

@webhook_bp.route(
    "/facebook",
    methods=["GET", "POST"],
)
def facebook_webhook():

    # --------------------------------------------------------
    # GET - WEBHOOK VERIFICATION
    # --------------------------------------------------------

    if request.method == "GET":

        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge", "")

        if (
            VERIFY_TOKEN
            and mode == "subscribe"
            and token == VERIFY_TOKEN
        ):
            logger.info(
                "[FB WEBHOOK] Verification successful"
            )

            return challenge, 200

        logger.warning(
            "[FB WEBHOOK] Verification failed | mode=%s",
            mode,
        )

        abort(403)

    # --------------------------------------------------------
    # POST - RECEIVE MESSENGER EVENTS
    # --------------------------------------------------------

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        logger.warning(
            "[FB WEBHOOK] Invalid JSON payload"
        )
        return "EVENT_RECEIVED", 200

    entries = payload.get("entry", [])

    if not isinstance(entries, list):
        logger.warning(
            "[FB WEBHOOK] Invalid entries format"
        )
        return "EVENT_RECEIVED", 200

    logger.info(
        "[FB WEBHOOK] POST received | "
        "object=%s | entries=%d",
        payload.get("object"),
        len(entries),
    )

    # --------------------------------------------------------
    # INSPECT FACEBOOK IDENTIFIERS
    # --------------------------------------------------------

    for entry in entries:

        if not isinstance(entry, dict):
            continue

        page_id = entry.get("id")
        messaging_events = entry.get("messaging", [])

        if not isinstance(messaging_events, list):
            continue

        for event in messaging_events:

            if not isinstance(event, dict):
                continue

            sender_id = (
                event.get("sender") or {}
            ).get("id")

            recipient_id = (
                event.get("recipient") or {}
            ).get("id")

            message = event.get("message") or {}

            message_id = (
                message.get("mid")
                if isinstance(message, dict)
                else None
            )

            # Do not log message text or tokens.
            logger.info(
                "[FB WEBHOOK] Message metadata | "
                "page_id=%s | sender_id=%s | "
                "recipient_id=%s | message_id=%s",
                page_id,
                sender_id,
                recipient_id,
                message_id,
            )

    # --------------------------------------------------------
    # PROCESS FACEBOOK PAYLOAD
    # --------------------------------------------------------

    app = current_app._get_current_object()

    try:
        webhook_executor.submit(
            process_facebook_payload,
            payload,
            app,
        )

    except RuntimeError:
        logger.exception(
            "[FB WEBHOOK] Failed to submit payload"
        )
        return "PROCESSING_UNAVAILABLE", 503

    return "EVENT_RECEIVED", 200


# ============================================================
# WAHA WHATSAPP WEBHOOK
# ============================================================

@webhook_bp.route(
    "/waha",
    methods=["POST"],
)
def waha_webhook():

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict) or not payload:
        return "OK", 200

    logger.info(
        "[WAHA WEBHOOK] POST received | event=%s",
        payload.get("event"),
    )

    app = current_app._get_current_object()

    try:
        webhook_executor.submit(
            process_waha_payload,
            payload,
            app,
        )

    except RuntimeError:
        logger.exception(
            "[WAHA WEBHOOK] Failed to submit payload"
        )
        return "PROCESSING_UNAVAILABLE", 503

    return "OK", 200
