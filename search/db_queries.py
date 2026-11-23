import json
import logging
from typing import Any

from sqlalchemy.orm import selectinload

from models.models import LabService, db

logger = logging.getLogger(__name__)


def _parse_json_field(value: Any) -> list[str]:
    """Convert a JSON list field into a clean list of strings."""

    if not value:
        return []

    if isinstance(value, list):
        return [
            str(item).strip()
            for item in value
            if item
        ]

    if isinstance(value, str):
        try:
            parsed = json.loads(value)

            if isinstance(parsed, list):
                return [
                    str(item).strip()
                    for item in parsed
                    if item
                ]

        except (json.JSONDecodeError, TypeError):
            return [value.strip()]

    return []


def get_all_active_lab_services_for_search(laboratory_id: int) -> list[dict]:
    """Load all lab services of a laboratory and prepare fields needed by the search pipeline.
    The catalog is uniform across cities, so no city filter here."""
    if not laboratory_id:
        raise ValueError("laboratory_id is required")

    services = []

    try:
        lab_services = db.session.query(LabService).filter(
            LabService.laboratory_id == laboratory_id,
        ).all()

        for service in lab_services:
            services.append(
                {
                    "id": service.id,
                    "name": service.name,
                    "description": service.description or "",
                    "aliases": _parse_json_field(
                        service.alias_name
                    ),
                    "keywords": _parse_json_field(
                        service.keywords
                    ),
                }
            )

    except Exception as exc:
        logger.exception(
            "Error fetching lab services for search: %s",
            exc,
        )
        raise

    return services


def get_lab_services_by_ids(
    ids: list,
    laboratory_id: int,
) -> list[LabService]:
    """Load lab-scoped service records in the ranking order supplied by the search.
    city_prices are eager-loaded so get_price_for_city() doesn't trigger one query per service."""

    if not laboratory_id:
        raise ValueError("laboratory_id is required")
    if not ids:
        return []

    clean_ids = [
        int(item)
        for item in ids
        if str(item).isdigit()
    ]

    if not clean_ids:
        return []

    try:
        rows = (
            LabService.query
            .options(selectinload(LabService.city_prices))
            .filter(
                LabService.id.in_(clean_ids),
                LabService.laboratory_id == laboratory_id,
            )
            .all()
        )
        rows_by_id = {row.id: row for row in rows}
        missing_ids = [service_id for service_id in clean_ids if service_id not in rows_by_id]
        if missing_ids:
            logger.warning(
                "Search result IDs missing from DB lookup | lab=%s ids=%s",
                laboratory_id, missing_ids,
            )
        return [rows_by_id[service_id] for service_id in clean_ids if service_id in rows_by_id]

    except Exception:
        logger.exception(
            "Error fetching lab services by IDs: %s",
            clean_ids,
        )
        raise