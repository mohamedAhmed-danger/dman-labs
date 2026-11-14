import logging
import os

logger = logging.getLogger(__name__)


def resolve_base_url(page_id):
    """Pick the right WAHA container for this page, via env-var mapping.

    Each WAHAx_PAGE_ID can hold several ids separated by commas (lid and c.us).
    """
    for i in range(1, 6):
        ids = os.environ.get(f"WAHA{i}_PAGE_ID", "")
        base_url = os.environ.get(f"WAHA{i}_BASE_URL")
        allowed = {x.strip() for x in ids.split(",") if x.strip()}
        if page_id and base_url and page_id in allowed:
            return base_url.rstrip("/")

    logger.error("[WAHA] No instance mapping for page_id=%s", page_id)
    raise ValueError(f"No WAHA instance mapped for page_id={page_id}")