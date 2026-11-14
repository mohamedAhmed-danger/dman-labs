import logging

from schemas.incoming_message import IncomingMessage

logger = logging.getLogger(__name__)


def _is_sticker(att: dict, msg_sticker_id) -> bool:
    """True for stickers and the blue thumbs-up like."""
    payload = att.get("payload")
    return (
        bool(msg_sticker_id)
        or att.get("type") == "sticker"
        or (isinstance(payload, dict) and bool(payload.get("sticker_id")))
    )


def parse_facebook_message(
    messaging,
    page_id,
    platform_id,
    platform_name: str = "Facebook",
) -> list[IncomingMessage]:
    try:
        if "delivery" in messaging or "read" in messaging:
            logger.debug("[FB PARSER] Ignoring delivery/read receipt")
            return []

        if "message" not in messaging:
            logger.debug("[FB PARSER] No 'message' key in event")
            return []

        msg = messaging["message"]

        if msg.get("is_echo", False):
            logger.debug("[FB PARSER] Ignoring echo message")
            return []

        sender_id = messaging.get("sender", {}).get("id")
        if not sender_id:
            logger.debug("[FB PARSER] Missing sender.id in event")
            return []

        msg_sticker_id = msg.get("sticker_id")
        attachments = msg.get("attachments")

        # Rare case: sticker_id on the message with no attachments
        if not attachments and msg_sticker_id:
            attachments = [{"type": "sticker", "payload": {"sticker_id": msg_sticker_id}}]

        parsed = []
        if attachments:
            parsed.extend(
                IncomingMessage(
                    sender_id=sender_id,
                    page_id=page_id,
                    platform_id=platform_id,
                    platform_name=platform_name,
                    msg_type="text",
                    text="👍",
                )
                if _is_sticker(att, msg_sticker_id)
                else IncomingMessage(
                    sender_id=sender_id,
                    page_id=page_id,
                    platform_id=platform_id,
                    platform_name=platform_name,
                    msg_type=att.get("type", "media"),
                    media=att.get("payload"),
                )
                for att in attachments
            )

            logger.debug(
                "[FB PARSER] Parsed %d attachment(s) sender_id=%s",
                len(attachments), sender_id,
            )

        text = msg.get("text")
        if text:
            image_message = next(
                (message for message in parsed if message.type == "image"),
                None,
            )
            if image_message:
                image_message.text = text
                logger.debug(
                    "[FB PARSER] Attached caption to image sender_id=%s len=%d",
                    sender_id, len(text),
                )
            else:
                parsed.append(IncomingMessage(
                    sender_id=sender_id,
                    page_id=page_id,
                    platform_id=platform_id,
                    platform_name=platform_name,
                    msg_type="text",
                    text=text,
                ))
                logger.debug(
                    "[FB PARSER] Parsed text message sender_id=%s len=%d",
                    sender_id, len(text),
                )

        return parsed

    except Exception:
        logger.exception("Fatal error in parse_facebook_message")
        return []


def parse_facebook_comment(change: dict) -> dict | None:
    try:
        if change.get("field") != "feed":
            return None

        value = change.get("value", {})

        if value.get("item") != "comment" or value.get("verb") != "add":
            return None

        

        comment_id = value.get("comment_id")
        if not comment_id:
            return None

        post_id = value.get("post_id")
        page_id = post_id.split("_")[0] if post_id else None

        return {
            "comment_id": comment_id,
            "page_id": page_id,
        }

    except Exception:
        logger.exception("Fatal error in parse_facebook_comment")
        return None




import logging
import re
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v25.0"


def get_facebook_conversation_link(
    page_id,
    sender_id,
    page_token,
    facebook_user_id=None,
):
    """
    Build a Meta Business Suite deep link.

    page_id:
        Facebook Page ID.

    sender_id:
        Page-Scoped ID (PSID) received from Messenger webhook.

    page_token:
        Page Access Token.

    facebook_user_id:
        Verified Facebook user ID, matching the
        messaging_actors.id observed in Business Suite.

    Returns:
        str | None

    IMPORTANT:
        The Graph API conversation "link" is not used
        as selected_item_id.
    """

    if not all([page_id, sender_id, page_token]):
        logger.warning(
            "[FACEBOOK] Missing required arguments"
        )
        return None

    page_id = str(page_id).strip()
    sender_id = str(sender_id).strip()

    # A PSID is not necessarily a Facebook profile ID.
    # Never substitute sender_id or conversation["link"]
    # for a missing verified Facebook user ID.

    if not facebook_user_id:
        logger.warning(
            "[FACEBOOK] No verified Business Suite user ID "
            "for page_id=%s sender_id=%s",
            page_id,
            sender_id,
        )
        return None

    facebook_user_id = str(facebook_user_id).strip()

    if not re.fullmatch(r"\d+", facebook_user_id):
        logger.warning(
            "[FACEBOOK] Invalid Facebook user ID"
        )
        return None

    graph_url = (
        f"https://graph.facebook.com/{GRAPH_VERSION}/"
        f"{page_id}/conversations"
    )

    try:
        # Check that the PSID has a conversation
        # with the requested Facebook Page.

        response = requests.get(
            graph_url,
            headers={
                "Authorization": f"Bearer {page_token}"
            },
            params={
                "user_id": sender_id,
                "fields": "id,updated_time",
                "limit": 1,
            },
            timeout=10,
        )

        response.raise_for_status()

        conversations = response.json().get("data", [])

        if not conversations:
            logger.warning(
                "[FACEBOOK] Conversation not found | "
                "page_id=%s sender_id=%s",
                page_id,
                sender_id,
            )
            return None

        # The verified Facebook user ID must come from
        # a trusted mapping for this exact page and PSID.
        # The Graph API lookup above does NOT verify
        # that mapping.

        params = urlencode({
            "nav_ref": (
                "manage_page_ap_plus_inbox_message_button"
            ),
            "asset_id": page_id,
            "business_id": "",
            "mailbox_id": page_id,
            "selected_item_id": facebook_user_id,
            "thread_type": "FB_MESSAGE",
        })

        business_link = (
            "https://business.facebook.com/"
            f"latest/inbox/all/?{params}"
        )

        logger.info(
            "[FACEBOOK] Generated Business Suite link | "
            "page_id=%s sender_id=%s "
            "facebook_user_id=%s",
            page_id,
            sender_id,
            facebook_user_id,
        )

        return business_link

    except requests.RequestException:
        logger.exception(
            "[FACEBOOK] Conversation verification failed"
        )
        return None

    except ValueError:
        logger.exception(
            "[FACEBOOK] Invalid Graph API response"
        )
        return None
