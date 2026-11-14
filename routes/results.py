
# routes/results.py

import logging
import os
import re

import redis
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    current_app,
)
from flask_login import login_required

from models.models import (
    db,
    Status,
    Client,
    Page,
    Platform,
)

from services.domain.result_service import ResultService

from platforms.facebook.parser import (
    get_facebook_conversation_link,
)


logger = logging.getLogger(__name__)

bp = Blueprint(
    "results",
    __name__,
    url_prefix="/results",
)


# ============================================================
# FACEBOOK USER ID MAPPING
# ============================================================

def get_verified_facebook_user_id(page_id, sender_id):
    """
    Retrieve a previously verified Business Suite user ID.

    Mapping:
        (page_id, sender_id) -> facebook_user_id

    The mapping must be verified for the same
    customer and same Facebook Page.

    No database schema changes.
    """

    redis_url = (
        current_app.config.get("REDIS_URL")
        or os.environ.get("REDIS_URL")
    )

    if not redis_url:
        logger.warning(
            "[FACEBOOK] REDIS_URL not configured"
        )
        return None

    redis_key = (
        f"facebook:business_suite_user_id:"
        f"{page_id}:{sender_id}"
    )

    try:
        redis_client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )

        facebook_user_id = redis_client.get(redis_key)

        if not facebook_user_id:
            logger.info(
                "[FACEBOOK] No verified user mapping | "
                "page_id=%s sender_id=%s",
                page_id,
                sender_id,
            )
            return None

        facebook_user_id = str(
            facebook_user_id
        ).strip()

        if not re.fullmatch(r"\d+", facebook_user_id):
            logger.warning(
                "[FACEBOOK] Invalid stored user ID | "
                "page_id=%s sender_id=%s",
                page_id,
                sender_id,
            )
            return None

        return facebook_user_id

    except (redis.RedisError, ValueError):
        logger.exception(
            "[FACEBOOK] Redis mapping lookup failed"
        )
        return None


# ============================================================
# RESULTS LIST
# ============================================================

@bp.route("/")
@login_required
def list_results():

    search = request.args.get("search") or None
    status = request.args.get("status") or None
    page_number = request.args.get(
        "page",
        1,
        type=int,
    )

    pagination = ResultService.list_requests(
        status=status,
        search=search,
        page=page_number,
    )

    results = pagination.items

    messenger_links = {}
    facebook_customers = {}

    for result in results:

        identity = str(
            result.user_identity or ""
        ).strip()

        if not identity:
            continue

        try:

            matches = (
                db.session.query(
                    Client,
                    Page,
                    Platform,
                )
                .join(
                    Page,
                    (Client.page_id == Page.page_id)
                    & (
                        Client.platform_id
                        == Page.platform_id
                    )
                )
                .join(
                    Platform,
                    Platform.id == Client.platform_id,
                )
                .filter(
                    Client.sender_id == identity,
                    Platform.name.ilike("%facebook%"),
                    Page.laboratory_id
                    == result.laboratory_id,
                )
                .limit(2)
                .all()
            )

            # Never guess when multiple clients match.
            if len(matches) != 1:
                continue

            client, page, platform = matches[0]

            page_id = str(page.page_id)
            sender_id = str(client.sender_id)

            # Used by the list template.
            facebook_customers[result.id] = {
                "name": None,
                "sender_id": sender_id,
                "page_id": page_id,
            }

            # Retrieve a VERIFIED Facebook profile ID.
            facebook_user_id = (
                get_verified_facebook_user_id(
                    page_id=page_id,
                    sender_id=sender_id,
                )
            )

            if not facebook_user_id:
                # No valid user ID means no direct link.
                # Do not generate a misleading URL.
                continue

            messenger_url = (
                get_facebook_conversation_link(
                    page_id=page_id,
                    sender_id=sender_id,
                    page_token=page.token,
                    facebook_user_id=facebook_user_id,
                )
            )

            if messenger_url:

                messenger_links[result.id] = (
                    messenger_url
                )

                logger.info(
                    "[RESULTS] Messenger link ready | "
                    "request_id=%s | "
                    "page_id=%s | "
                    "sender_id=%s",
                    result.id,
                    page_id,
                    sender_id,
                )

        except Exception:
            logger.exception(
                "[RESULTS] Failed to resolve "
                "Facebook customer | "
                "request_id=%s",
                result.id,
            )

    return render_template(
        "results/list.html",
        results=results,
        pagination=pagination,
        search=search,
        status=status,
        Status=Status,
        messenger_links=messenger_links,
        facebook_customers=facebook_customers,
    )


# ============================================================
# UPDATE RESULT STATUS
# ============================================================

@bp.route(
    "/<int:request_id>/status",
    methods=["POST"],
)
@login_required
def update_result_status(request_id):

    ResultService.update_status(
        request_id,
        request.form.get("status_val"),
    )

    flash(
        "تم تحديث حالة الطلب",
        "success",
    )

    return redirect(
        request.referrer
        or url_for("results.list_results")
    )


# ============================================================
# DELETE RESULT REQUEST
# ============================================================

@bp.route(
    "/<int:request_id>/delete",
    methods=["POST"],
)
@login_required
def delete_result(request_id):

    ResultService.delete(request_id)

    flash(
        "تم حذف الطلب",
        "success",
    )

    return redirect(
        url_for("results.list_results")
    )
