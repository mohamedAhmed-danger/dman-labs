import logging

from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, PointStruct, VectorParams

from config import Config
from notification_center import send_production_alert
from utils.embedding_utils import build_embedding_text, create_embedding

logger = logging.getLogger(__name__)

qdrant_client = QdrantClient(url=Config.QDRANT_URL, api_key=Config.QDRANT_API_KEY)

# collections we already verified in this process (avoids a network call per upsert)
_ensured_collections: set[str] = set()


def get_collection_name(laboratory_id: int) -> str:
    """Returns the Qdrant collection name for a laboratory (one collection per lab)."""
    if not laboratory_id:
        raise ValueError("laboratory_id is required to compute collection name")
    return f"tests_lab{laboratory_id}"


def ensure_collection(laboratory_id: int) -> str:
    """Ensures the Qdrant collection for a laboratory exists, creating it if missing."""
    if not laboratory_id:
        raise ValueError("laboratory_id is required")
    col_name = get_collection_name(laboratory_id)
    if col_name in _ensured_collections:
        return col_name
    try:
        existing = {c.name for c in qdrant_client.get_collections().collections}
        if col_name not in existing:
            qdrant_client.create_collection(
                collection_name=col_name,
                vectors_config=VectorParams(size=Config.VECTOR_SIZE, distance=Distance.COSINE),
            )
            logger.info("Qdrant collection '%s' created successfully.", col_name)
        _ensured_collections.add(col_name)
        return col_name
    except Exception as e:
        logger.exception("Failed to ensure Qdrant collection %s", col_name)
        try:
            send_production_alert(
                subject=f"Qdrant Collection Creation Failure ({col_name})",
                body_or_error=e,
                context={"collection_name": col_name, "qdrant_url": Config.QDRANT_URL},
            )
        except Exception:
            pass
        raise


def upsert_test_vector(test_id: int, test_name: str, description: str, keywords: list[str],
                       laboratory_id: int) -> bool:
    """Creates or updates the vector for a test in its laboratory collection."""
    if not laboratory_id:
        raise ValueError("laboratory_id is required")

    keywords = keywords or []

    try:
        vector = create_embedding(build_embedding_text(test_name, description or "", keywords))
    except ValueError as e:
        logger.warning("Skipping vector upsert for test %s: %s", test_id, e)
        return False

    # No price and no city in the payload: price is resolved from SQL at answer time.
    payload = {
        "service_id": test_id,
        "name": test_name,
        "laboratory_id": laboratory_id,
    }

    target_collection = ensure_collection(laboratory_id)

    try:
        qdrant_client.upsert(
            collection_name=target_collection,
            points=[PointStruct(id=test_id, vector=vector, payload=payload)],
            wait=True,
        )
    except Exception as e:
        logger.exception("Qdrant upsert failed for test %s in collection %s", test_id, target_collection)
        try:
            send_production_alert(
                subject=f"Qdrant Upsert Failure (Test ID: {test_id})",
                body_or_error=e,
                context={"test_id": test_id, "test_name": test_name, "collection": target_collection},
            )
        except Exception:
            pass
        raise

    logger.info("Test ID %s upserted successfully into Qdrant collection %s.", test_id, target_collection)
    return True


def upsert_test_vectors_batch(laboratory_id: int, items: list[dict], batch_size: int = 100) -> dict:
    """
    Upserts a batch of test vectors into the laboratory collection in chunks (default 100).
    Each item: test_id (int), test_name (str), description (str, optional), keywords (list, optional).

    Returns: {"succeeded": [test_ids], "failed": [{"test_id": ..., "error": ...}]}
    """
    if not laboratory_id:
        raise ValueError("laboratory_id is required")

    succeeded = []
    failed = []

    if not items:
        return {"succeeded": succeeded, "failed": failed}

    target_collection = ensure_collection(laboratory_id)
    points_to_upsert = []

    for item in items:
        test_id = item.get("test_id")
        test_name = item.get("test_name", "")
        description = item.get("description") or ""
        keywords = item.get("keywords") or []

        try:
            vector = create_embedding(build_embedding_text(test_name, description, keywords))
            payload = {
                "service_id": test_id,
                "name": test_name,
                "laboratory_id": laboratory_id,
            }
            points_to_upsert.append((test_id, PointStruct(id=test_id, vector=vector, payload=payload)))
        except Exception as e:
            logger.warning("Failed to generate embedding for test %s in batch: %s", test_id, e)
            failed.append({"test_id": test_id, "error": str(e)})

    for i in range(0, len(points_to_upsert), batch_size):
        chunk_tuples = points_to_upsert[i:i + batch_size]
        chunk_points = [pt for _, pt in chunk_tuples]
        try:
            qdrant_client.upsert(
                collection_name=target_collection,
                points=chunk_points,
                wait=True,
            )
            succeeded.extend([tid for tid, _ in chunk_tuples])
        except Exception as e:
            logger.exception("Qdrant batch upsert failed for chunk starting at index %d in %s", i, target_collection)
            for tid, _ in chunk_tuples:
                failed.append({"test_id": tid, "error": str(e)})

    logger.info("Batch upsert to %s completed: %d succeeded, %d failed", target_collection, len(succeeded), len(failed))
    return {"succeeded": succeeded, "failed": failed}


def delete_test_vector(test_id: int, laboratory_id: int) -> bool:
    """Deletes a test vector from the laboratory collection."""
    if not laboratory_id:
        raise ValueError("laboratory_id is required")

    target_collection = get_collection_name(laboratory_id)

    try:
        qdrant_client.delete(
            collection_name=target_collection,
            points_selector=[test_id],
        )
    except Exception as e:
        logger.exception("Qdrant delete failed for test %s in collection %s", test_id, target_collection)
        try:
            send_production_alert(
                subject=f"Qdrant Delete Failure (Test ID: {test_id})",
                body_or_error=e,
                context={"test_id": test_id, "collection": target_collection},
            )
        except Exception:
            pass
        raise

    logger.info("Test ID %s deleted from Qdrant collection %s.", test_id, target_collection)
    return True


def get_test_vector(test_id: int, laboratory_id: int):
    """Retrieves a test vector by ID from the laboratory collection."""
    if not laboratory_id:
        raise ValueError("laboratory_id is required")

    target_collection = get_collection_name(laboratory_id)

    try:
        result = qdrant_client.retrieve(
            collection_name=target_collection,
            ids=[test_id],
        )
    except Exception:
        logger.exception("Qdrant retrieve failed for test %s", test_id)
        raise

    if not result:
        return None

    return result[0]