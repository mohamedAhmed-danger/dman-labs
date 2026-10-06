import logging

from schemas.incoming_message import IncomingMessage

logger = logging.getLogger(__name__)


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

        text = msg.get("text")
        if text:
            logger.debug(
                "[FB PARSER] Parsed text message sender_id=%s len=%d",
                sender_id, len(text),
            )
            return [IncomingMessage(
                sender_id=sender_id,
                page_id=page_id,
                platform_id=platform_id,
                platform_name=platform_name,
                msg_type="text",
                text=text,
            )]

        attachments = msg.get("attachments")
        if attachments:
            # رسالة مستقلة لكل attachment
            parsed = [
                IncomingMessage(
                    sender_id=sender_id,
                    page_id=page_id,
                    platform_id=platform_id,
                    platform_name=platform_name,
                    msg_type=att.get("type", "media"),
                    media=att.get("payload"),
                )
                for att in attachments
            ]
            logger.debug(
                "[FB PARSER] Parsed %d attachment(s) sender_id=%s",
                len(parsed), sender_id,
            )
            return parsed

        return []

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

        if value.get("parent_id") and value.get("parent_id") != value.get("post_id"):
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