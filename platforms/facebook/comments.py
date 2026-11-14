import logging
import random

from notification_center import send_production_alert
from platforms.http_client import safe_post
from platforms import http_client

logger = logging.getLogger(__name__)

PUBLIC_REPLIES = [
    "شكراً لتعليقك الجميل 🌷 تم الرد عليك خاص من معامل ضمان 💙",
    "نورتنا 💙 تم الرد عليك على الخاص من معامل ضمان 🌷",
    "شكراً لتواصلك مع معامل ضمان 🙏 تم الرد خاص، تقدر تكمل معانا هناك 💙",
]

PRIVATE_REPLIES = [
    "أهلاً بيك في معامل ضمان 💙 معاك المساعد الافتراضي لمعامل ضمان، شكراً لتعليقك! أقدر أساعدك إزاي النهاردة؟",

    "نورتنا يا فندم 🌷 أنا المساعد الافتراضي لمعامل ضمان، تحب تحجز تحاليل ولا تستفسر عن حاجة؟",

    "أهلاً وسهلاً 💙 معاك المساعد الافتراضي لمعامل ضمان، موجود علشان أساعدك في أي استفسار أو حجز تحاليل. أقدر أخدمك بإيه؟",
]

# قيم افتراضية احتياطية
DEFAULT_PUBLIC_REPLY = PUBLIC_REPLIES[0]
DEFAULT_PRIVATE_REPLY = PRIVATE_REPLIES[0]


def get_public_reply() -> str:
    return random.choice(PUBLIC_REPLIES)


def get_private_reply() -> str:
    return random.choice(PRIVATE_REPLIES)


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _diagnose(code, subcode, status):
    if code == 190:
        return "TOKEN: الـ page token منتهي أو غلط أو اتلغى"
    if code in (10, 200) or (code and 200 <= code <= 299):
        return "PERMISSION: ناقص صلاحية (pages_manage_engagement للايك والرد العام / pages_messaging للخاص) أو الـ app في Dev mode"
    if code == 100:
        return "INVALID PARAM: التعليق اتمسح، أو مش top-level، أو الـ ID غلط، أو الـ endpoint غلط"
    if code in (4, 17, 32, 613):
        return "RATE LIMIT: ضغط على الـ API"
    if code == 368:
        return "BLOCKED: فيسبوك حظر الإجراء مؤقتاً (سبام)"
    if status in (401, 403):
        return "AUTH/PERMISSION"
    return "UNKNOWN: راجع message و fbtrace_id"


def _log_result(step, comment_id, page_id, response):
    if response is None:
        logger.error(
            "[FB %s] NO RESPONSE (timeout/connection) | comment_id=%s page_id=%s",
            step, comment_id, page_id,
        )
        return
    if response.ok:
        logger.info(
            "[FB %s] OK status=%s | comment_id=%s | body=%s",
            step, response.status_code, comment_id, response.text[:200],
        )
        return
    try:
        err = response.json().get("error", {})
    except ValueError:
        err = {"message": response.text[:300]}
    logger.error(
        "[FB %s] FAILED status=%s code=%s subcode=%s type=%s message=%s fbtrace_id=%s | comment_id=%s page_id=%s | => %s",
        step, response.status_code, err.get("code"), err.get("error_subcode"),
        err.get("type"), err.get("message"), err.get("fbtrace_id"),
        comment_id, page_id, _diagnose(err.get("code"), err.get("error_subcode"), response.status_code),
    )


def react_to_comment(comment_id: str, base_url: str, headers: dict):
    """لايك على التعليق. ثانوي: لو فشل بيتسجل بس من غير alert."""
    logger.debug("[FB LIKE COMMENT] comment_id=%s", comment_id)
    return safe_post(
        f"{base_url}/{comment_id}/likes",
        label="Facebook Like",
        alert=False,
        headers=headers,
        timeout=(5, 10),
    )


def reply_to_comment(
    comment_id: str,
    base_url: str,
    headers: dict,
    page_id,
    static_message: str | None = None,
):
    """رد عام على التعليق (بيختار رد عشوائي لو مفيش رسالة محددة)."""
    message = static_message or get_public_reply()
    logger.debug("[FB COMMENT REPLY] comment_id=%s", comment_id)
    return safe_post(
        f"{base_url}/{comment_id}/comments",
        label="Facebook Comment Reply",
        page_id=page_id,
        json={"message": message},
        headers=headers,
    )


def send_private_reply(page_id, page_access_token: str, comment_id: str, text: str, base_url: str) -> dict:
    """رسالة خاصة على التعليق. يرجّع dict فيه status و ok و data."""
    logger.debug("[FB PRIVATE REPLY] comment_id=%s page_id=%s", comment_id, page_id)

    response = safe_post(
        f"{base_url}/{page_id}/messages",
        label="Facebook Private Reply",
        page_id=page_id,
        json={
            "recipient": {"comment_id": comment_id},
            "message": {"text": text},
            "messaging_type": "RESPONSE",
        },
        headers=_auth_headers(page_access_token),
    )
    if response is None:
        return {"status": 500, "ok": False, "data": "connection error"}

    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text[:500]}
    return {"status": response.status_code, "ok": response.ok, "data": data}


def is_reply_to_comment(comment_id: str, base_url: str, headers: dict) -> bool:
    """True لو التعليق ده رد على تعليق تاني."""
    try:
        r = http_client.session.get(
            f"{base_url}/{comment_id}",
            params={"fields": "parent{id}"},
            headers=headers,
            timeout=(5, 10),
        )
        r.raise_for_status()
        return "parent" in r.json()
    except Exception:
        logger.exception("[FB COMMENT] parent check failed | comment_id=%s", comment_id)
        return False  # لو الفحص فشل نرد برضه

def handle_comment(comment_id: str, page_id: str, base_url: str, token: str):
    """لايك + رد عام + رسالة خاصة، كل خطوة مستقلة ومتسجلة."""
    headers = _auth_headers(token)
    logger.info("[FB HANDLE COMMENT] START | comment_id=%s page_id=%s", comment_id, page_id)
    if is_reply_to_comment(comment_id, base_url, headers):
        logger.info("[FB HANDLE COMMENT] SKIPPED: reply to a comment | comment_id=%s", comment_id)
        return

    steps = [
        ("Like", lambda: react_to_comment(comment_id, base_url, headers)),
        ("Public Reply", lambda: reply_to_comment(comment_id, base_url, headers, page_id)),
        ("Private Reply", lambda: send_private_reply(page_id, token, comment_id, get_private_reply(), base_url)),
    ]

    for step, action in steps:
        try:
            result = action()
            if isinstance(result, dict):  # send_private_reply بيرجّع dict
                if result["ok"]:
                    logger.info("[FB %s] OK | comment_id=%s", step, comment_id)
                else:
                    err = (result["data"] or {}).get("error", {}) if isinstance(result["data"], dict) else {}
                    logger.error(
                        "[FB %s] FAILED status=%s code=%s subcode=%s message=%s | comment_id=%s | => %s",
                        step, result["status"], err.get("code"), err.get("error_subcode"),
                        err.get("message"), comment_id,
                        _diagnose(err.get("code"), err.get("error_subcode"), result["status"]),
                    )
            else:
                _log_result(step, comment_id, page_id, result)
        except Exception as e:
            logger.exception("[FB %s] EXCEPTION | comment_id=%s", step, comment_id)
            send_production_alert(
                subject=f"Facebook {step} Failure",
                body_or_error=e,
                context={"comment_id": comment_id, "page_id": page_id},
            )
