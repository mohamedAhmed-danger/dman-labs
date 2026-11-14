import logging
from typing import Any, Dict, List, Tuple

from schemas.intent import RefinedQuery
from models.models import City
from search.db_queries import get_lab_services_by_ids
from search.search_manager import run_search

logger = logging.getLogger(__name__)


def _normalize_queries(
    refined_queries: List[Any],
) -> List[RefinedQuery]:
    """Convert raw refined queries into RefinedQuery objects."""
    typed_queries = []

    logger.info("🔍 [CONTEXT BUILDER] Normalizing %d raw queries...", len(refined_queries))

    for idx, query in enumerate(refined_queries, 1):
        if isinstance(query, dict):
            try:
                converted = RefinedQuery(**query)
                typed_queries.append(converted)
                logger.info("  ├─ Query #%d (dict) -> %s", idx, converted.query)
            except Exception:
                logger.exception(
                    "  ├─ Failed to convert refined query #%d | query=%s",
                    idx,
                    query,
                )
        elif isinstance(query, RefinedQuery):
            typed_queries.append(query)
            logger.info("  ├─ Query #%d (RefinedQuery) -> %s", idx, query.query)
        else:
            logger.warning(
                "  ├─ Ignoring unsupported refined query type #%d: %s",
                idx,
                type(query).__name__,
            )

    logger.info("✅ [CONTEXT BUILDER] Normalized queries count: %d", len(typed_queries))
    return typed_queries


class ContextBuilder:
    """Build the RAG context used by downstream LLM nodes."""

    @staticmethod
    def format_search_results_to_context(
        results: List[Any],
        lab_services_by_id: Dict[int, Any],
        laboratory_id: int,
        city_id: int,
    ) -> str:
        """Format search results and lab data into LLM-readable context."""
        if not laboratory_id or not city_id:
            raise ValueError("laboratory_id and city_id are required for context formatting")
        city = City.query.filter_by(id=city_id, laboratory_id=laboratory_id).first()
        if not city:
            raise ValueError("City does not belong to the requested laboratory")
        unavailable = (
            f"Client region: {city.name}\n"
            "No matching test in the client's region. Do not use information or prices from other regions."
        )
        if not results:
            logger.warning("⚠️ [CONTEXT BUILDER] No results provided to format_search_results_to_context.")
            return unavailable

        context_blocks = [f"Client region: {city.name}"]

        for idx, result in enumerate(results, 1):
            lab = lab_services_by_id.get(result.id)
            if not lab:
                logger.warning(
                    "⚠️ [CONTEXT BUILDER] Lab service not found in DB | id=%s",
                    result.id,
                )
                continue
            if lab.laboratory_id != laboratory_id or lab.city_id != city_id:
                logger.warning(
                    "Refusing out-of-scope test row in context | id=%s lab=%s city=%s",
                    result.id, laboratory_id, city_id,
                )
                continue

            aliases = lab.alias_name or []
            if isinstance(aliases, str):
                aliases = [aliases]

            aliases_text = (
                ", ".join(str(alias) for alias in aliases)
                if aliases
                else "None"
            )

            block = (
                f"--- Result #{idx} ---\n"
                f"Test ID: {lab.id}\n"
                f"Test Name: {lab.name}\n"
                f"Aliases: {aliases_text}\n"
                f"Description: {lab.description or 'Not available'}\n"
                f"Price: {lab.price:.2f} EGP\n"
                f"Duration: "
                f"{lab.duration if lab.duration is not None else 'Not available'}\n"
                f"Sample Type: {lab.sample_type or 'Not available'}\n"
                f"Patient Instructions: "
                f"{lab.patient_instructions or 'No specific instructions'}\n"
                f"Match Confidence Score: {result.score:.2f}\n"
                f"Search Source: {result.source}"
            )
            context_blocks.append(block)

        if len(context_blocks) == 1:
            logger.warning("⚠️ [CONTEXT BUILDER] All results failed DB lookup, empty context produced.")
            return unavailable

        final_context = "\n\n".join(context_blocks)
        logger.info(
            "📄 [CONTEXT BUILDER] Formatted Context built | blocks=%d | total_chars=%d",
            len(context_blocks),
            len(final_context),
        )
        return final_context

    @classmethod
    def build_rag_context(
        cls,
        refined_queries: List[Any],
        laboratory_id: int,
        city_id: int,
    ) -> Tuple[str, List[Any], float]:
        """
        Run the complete RAG search pipeline:
        Normalize Queries -> Search -> Fetch Lab Data -> Build Context.
        """
        if not laboratory_id or not city_id:
            raise ValueError("laboratory_id and city_id are required for RAG context")
        logger.info("🚀 [RAG PIPELINE START] Input raw queries count: %d", len(refined_queries or []))

        if not refined_queries:
            logger.info("ℹ️ [RAG PIPELINE] Empty refined_queries list received.")
            return "", [], 0.0

        typed_queries = _normalize_queries(refined_queries)
        if not typed_queries:
            logger.warning("⚠️ [RAG PIPELINE] All queries failed normalization.")
            return "", [], 0.0

        # 1. Run search
        try:
            logger.info("🔎 [RAG PIPELINE] Running search across %d normalized queries...", len(typed_queries))
            search_output = run_search(typed_queries, laboratory_id, city_id)
            results = search_output.get("results", [])
            top_score = search_output.get("top_score", 0.0)
            logger.info("🎯 [RAG PIPELINE] Search completed | raw_results=%d | top_score=%.4f", len(results), top_score)
        except Exception:
            logger.exception("[RAG PIPELINE] Search pipeline execution failed completely.")
            return (
                "Failed to retrieve laboratory test information.",
                [],
                0.0,
            )

        if not results:
            logger.info("⚠️ [RAG PIPELINE] Search returned 0 matching results.")
            context = cls.format_search_results_to_context([], {}, laboratory_id, city_id)
            return (
                context,
                [],
                0.0,
            )

        # 2. Fetch complete lab records from database
        test_ids = list(dict.fromkeys(result.id for result in results))
        logger.info("📦 [RAG PIPELINE] Fetching DB records for unique test IDs: %s", test_ids)

        try:
            lab_services = get_lab_services_by_ids(test_ids, laboratory_id, city_id)
            logger.info("✅ [RAG PIPELINE] Successfully fetched %d lab service records from DB.", len(lab_services))
        except Exception:
            logger.exception(
                "[RAG PIPELINE] Failed to retrieve lab services from DB | ids=%s",
                test_ids,
            )
            return (
                "Failed to retrieve laboratory test details.",
                [],
                top_score,
            )

        lab_services_by_id = {lab.id: lab for lab in lab_services}

        # 3. Build the final context for the LLM
        formatted_context = cls.format_search_results_to_context(
            results=results,
            lab_services_by_id=lab_services_by_id,
            laboratory_id=laboratory_id,
            city_id=city_id,
        )
        logger.info(
            "🏁 [RAG PIPELINE COMPLETE] final_context_length=%d | results=%d | top_score=%.4f",
            len(formatted_context),
            len(results),
            top_score,
        )

        return formatted_context, results, top_score