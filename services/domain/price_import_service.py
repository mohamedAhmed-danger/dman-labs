"""
price_import_service.py

Two-phase import: preview → commit.

Column names (matched case-insensitively):
    Required:   "Test Name" | "اسم التحليل" | "name"
                "Price"     | "السعر"       | "price"
    Optional:   "Test Details" | "تفاصيل التحليل" | "description"
                "Duration"     | "وقت الاستلام"   | "duration"
                "Sample Type"  | "نوع العينة"      | "sample_type"

Preview:
    - Parses the file; normalises names (strip + collapse whitespace + lowercase for matching).
    - Returns {new, price_changed, unchanged, needs_review, warnings, token}.
    - Saves parsed rows to Redis under a token (TTL 30 min).
    - Duplicate name inside the file: last row wins (warning emitted).

Commit:
    - One DB transaction: creates new rows, updates price+description for existing.
    - delete_missing=True → removes city tests absent from the file and their Qdrant points.
    - After commit, AI generation + embedding run in a background thread.
    - Background job progress is stored in Redis: import:progress:{token}.
    - Returns {token, created, updated, deleted, total_for_ai}.

Background job:
    - Calls generate_test() only for rows where fields are missing.
    - Rate-limited by AI_RATE_LIMIT_DELAY (default 1 s).
    - Failed AI or embedding → test.sync_status = "pending".
    - Successful embedding → test.sync_status = "ok".
    - Batch upserts in chunks of 100.

retry_pending(laboratory_id, city_id):
    - Reprocesses all sync_status="pending" tests of that city.

get_import_progress(token):
    - Reads the Redis progress hash for a commit token.
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from io import BytesIO
from typing import IO

from sqlalchemy.exc import IntegrityError

from config import Config
from models.models import db, LabService, City
from services.shared.generation_service import generate_test
from services.shared.vector_service import (
    upsert_test_vectors_batch,
    delete_test_vector,
    get_collection_name,
)
from services.messaging.redis_queue import get_redis_client

logger = logging.getLogger(__name__)

AI_RATE_LIMIT_DELAY = float(os.getenv("AI_RATE_LIMIT_DELAY", "1"))
PREVIEW_TTL_SECONDS = 1800  # 30 minutes

# ---------------------------------------------------------------------------
# Column aliases (lower-stripped matching)
# ---------------------------------------------------------------------------
_NAME_ALIASES = {"test name", "اسم التحليل", "name", "الاسم"}
_PRICE_ALIASES = {"price", "السعر", "سعر التحليل"}
_DESC_ALIASES = {"test details", "تفاصيل التحليل", "description", "وصف", "الوصف"}
_DURATION_ALIASES = {"duration", "وقت الاستلام", "مدة التحليل", "المدة", "hours"}
_SAMPLE_ALIASES = {"sample type", "نوع العينة", "sample_type", "العينة"}


def _col_match(header: str, aliases: set) -> bool:
    return header.strip().lower() in {a.lower() for a in aliases}


def _map_headers(headers: list[str]) -> dict:
    mapping = {"name": None, "price": None, "description": None,
               "duration": None, "sample_type": None}
    for idx, h in enumerate(headers):
        hl = (h or "").strip().lower()
        if _col_match(hl, _NAME_ALIASES):
            mapping["name"] = idx
        elif _col_match(hl, _PRICE_ALIASES):
            mapping["price"] = idx
        elif _col_match(hl, _DESC_ALIASES):
            mapping["description"] = idx
        elif _col_match(hl, _DURATION_ALIASES):
            mapping["duration"] = idx
        elif _col_match(hl, _SAMPLE_ALIASES):
            mapping["sample_type"] = idx
    return mapping


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def _read_excel(file_obj: IO[bytes]) -> tuple[list[str], list[list]]:
    import openpyxl
    wb = openpyxl.load_workbook(file_obj, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    return [str(c) if c is not None else "" for c in rows[0]], [list(r) for r in rows[1:]]


def _read_csv(file_obj: IO[bytes]) -> tuple[list[str], list[list]]:
    import csv, io
    content = file_obj.read()
    for enc in ("utf-8-sig", "utf-8", "cp1256"):
        try:
            text = content.decode(enc); break
        except UnicodeDecodeError:
            text = None
    if not text:
        raise ValueError("تعذّر قراءة ملف CSV — تأكد من Encoding")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], []
    return [c.strip() for c in rows[0]], [list(r) for r in rows[1:]]


def _cell_str(val) -> str:
    return str(val).strip() if val is not None else ""


def _normalize_name(raw: str) -> str:
    """Lowercase + collapse internal whitespace for matching only."""
    return re.sub(r"\s+", " ", raw.strip().lower())


def _desc_changed(new_desc: str | None, db_desc: str | None) -> bool:
    """
    Fix 1: compare descriptions after normalising whitespace.
    Returns True only when the content is meaningfully different.
    A None/empty new description never counts as a change.
    """
    if not new_desc:          # nothing in the file → no change
        return False
    norm_new = re.sub(r"\s+", " ", new_desc.strip())
    norm_db  = re.sub(r"\s+", " ", (db_desc or "").strip())
    return norm_new != norm_db


def _name_changed(new_name: str, db_name: str) -> bool:
    """Name comparison after whitespace normalisation."""
    return re.sub(r"\s+", " ", new_name.strip()) != re.sub(r"\s+", " ", (db_name or "").strip())


# Arabic-Indic to ASCII digit map
_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _cell_float(val) -> float | None:
    """
    Robust price parser:
    - Accepts 1,105.00  /  1105.00  /  1 105  (spaces)  /  Arabic-Indic digits
    - Returns None for empty / non-numeric / negative / zero values
    """
    if val is None:
        return None
    s = str(val).strip().translate(_ARABIC_INDIC)
    # Remove thousand-separator commas and spaces between digits
    s = re.sub(r"(?<=\d)[,\s](?=\d)", "", s)
    s = s.replace(",", "")  # any remaining commas
    s = s.strip()
    if not s:
        return None
    try:
        result = float(s)
        return result if result > 0 else None
    except (ValueError, TypeError):
        return None


def _cell_int(val) -> int | None:
    f = _cell_float(val)
    return int(f) if f is not None else None


def _get_cell(row, col_map, field):
    idx = col_map.get(field)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


# ---------------------------------------------------------------------------
# Redis helpers for preview token and progress
# ---------------------------------------------------------------------------
def _preview_key(token: str) -> str:
    return f"import:preview:{token}"


def _progress_key(token: str) -> str:
    return f"import:progress:{token}"


def _save_preview(token: str, data: dict) -> None:
    r = get_redis_client()
    r.set(_preview_key(token), json.dumps(data, ensure_ascii=False), ex=PREVIEW_TTL_SECONDS)


def _load_preview(token: str) -> dict | None:
    r = get_redis_client()
    raw = r.get(_preview_key(token))
    return json.loads(raw) if raw else None


def _init_progress(token: str, total: int) -> None:
    r = get_redis_client()
    r.hset(_progress_key(token), mapping={
        "total": total, "done": 0, "failed": 0, "pending": 0, "status": "running"
    })
    r.expire(_progress_key(token), PREVIEW_TTL_SECONDS)


def _update_progress(token: str, done: int = 0, failed: int = 0) -> None:
    r = get_redis_client()
    if done:
        r.hincrby(_progress_key(token), "done", done)
    if failed:
        r.hincrby(_progress_key(token), "failed", failed)


def _finish_progress(token: str) -> None:
    r = get_redis_client()
    r.hset(_progress_key(token), "status", "done")


# ---------------------------------------------------------------------------
# Public: get_import_progress
# ---------------------------------------------------------------------------
def get_import_progress(token: str) -> dict:
    """
    Return the background AI/embedding job progress for a commit token.
    Keys: total, done, failed, pending, status ('running' | 'done').
    Returns {} if the token has expired or never existed.
    """
    r = get_redis_client()
    data = r.hgetall(_progress_key(token))
    if not data:
        return {}
    return {k: int(v) if v.isdigit() else v for k, v in data.items()}


# ---------------------------------------------------------------------------
# Public: preview
# ---------------------------------------------------------------------------
def preview_import(
    file_obj: IO[bytes],
    filename: str,
    laboratory_id: int,
    city_id: int,
    delete_missing: bool = False,
) -> dict:
    """
    Parse the file, classify every row, save to Redis, return a token.

    Returns:
        {
            "token":           str,
            "new":             int,
            "price_changed":   int,
            "desc_changed":    int,
            "unchanged":       int,
            "needs_review":    list[{"row": int, "name": str, "reason": str}],
            "warnings":        list[str],
            "would_delete":    {"count": int, "names": list[str]},  # only when delete_missing=True
            "error":           str | None,
        }
    """
    result = {"token": None, "new": 0, "price_changed": 0, "desc_changed": 0,
              "unchanged": 0, "needs_review": [], "warnings": [],
              "would_delete": {"count": 0, "names": []}, "error": None}

    # Validate city ownership
    city = db.session.get(City, city_id)
    if not city:
        result["error"] = "المدينة غير موجودة"; return result
    if city.laboratory_id != laboratory_id:
        result["error"] = "المدينة لا تنتمي إلى هذا المعمل"; return result

    ext = (filename or "").rsplit(".", 1)[-1].lower()
    try:
        if ext in ("xlsx", "xls"):
            headers, rows = _read_excel(file_obj)
        elif ext == "csv":
            headers, rows = _read_csv(file_obj)
        else:
            result["error"] = f"نوع الملف '{ext}' غير مدعوم. استخدم xlsx أو csv."
            return result
    except Exception as e:
        result["error"] = f"خطأ في قراءة الملف: {e}"; return result

    if not headers or not rows:
        result["error"] = "الملف فارغ"; return result

    col_map = _map_headers(headers)
    if col_map["name"] is None:
        result["error"] = "لم يتم العثور على عمود 'Test Name'. تأكد من عنوان العمود."
        return result
    if col_map["price"] is None:
        result["error"] = "لم يتم العثور على عمود 'Price'. تأكد من عنوان العمود."
        return result

    # Build a lookup of existing tests in this city (normalised name → {id, price, description})
    existing = {
        _normalize_name(s.name): {
            "id": s.id,
            "price": float(s.price or 0),
            "name": s.name,
            "description": s.description,
        }
        for s in LabService.query.filter_by(
            laboratory_id=laboratory_id, city_id=city_id
        ).all()
    }

    # Deduplicate within file (last row wins)
    seen_names: dict[str, int] = {}  # normalised_name → row_idx (1-based in file)
    parsed_rows: list[dict] = []     # ordered list after dedup

    for row_idx, row in enumerate(rows, start=2):
        raw_name = _cell_str(_get_cell(row, col_map, "name"))
        if not raw_name:
            continue

        # Fix 4: name length guard
        if len(raw_name) > 255:
            result["needs_review"].append({
                "row": row_idx, "name": raw_name[:60] + "…",
                "reason": "اسم التحليل أطول من 255 حرفاً"
            })
            continue

        # Fix 4: price must parse to a positive number; raw value shown on failure
        raw_price_cell = _get_cell(row, col_map, "price")
        raw_price = _cell_float(raw_price_cell)

        norm_name = _normalize_name(raw_name)

        if raw_price is None:
            raw_val = _cell_str(raw_price_cell) if raw_price_cell is not None else "—"
            result["needs_review"].append({
                "row": row_idx, "name": raw_name,
                "reason": f"سعر غير صالح: '{raw_val}'"
            })
            continue

        if norm_name in seen_names:
            result["warnings"].append(
                f"اسم مكرر في الملف: '{raw_name}' (صف {seen_names[norm_name]} و{row_idx}) — سيُستخدم آخر صف"
            )
            # Remove the previous entry from parsed_rows
            parsed_rows = [r for r in parsed_rows if r["norm_name"] != norm_name]

        seen_names[norm_name] = row_idx

        description = _cell_str(_get_cell(row, col_map, "description")) or None
        duration = _cell_int(_get_cell(row, col_map, "duration"))
        sample_type = _cell_str(_get_cell(row, col_map, "sample_type")) or None

        parsed_rows.append({
            "row": row_idx,
            "name": raw_name,
            "norm_name": norm_name,
            "price": raw_price,
            "description": description,
            "duration": duration,
            "sample_type": sample_type,
        })

    # Classify rows (Fix 1: compare desc after whitespace-normalisation)
    token_rows = []
    for r in parsed_rows:
        norm = r["norm_name"]
        if norm in existing:
            ex = existing[norm]
            price_same = abs(ex["price"] - r["price"]) < 0.001
            desc_diff  = _desc_changed(r.get("description"), ex.get("description"))
            if price_same and not desc_diff:
                r["action"] = "unchanged"
                result["unchanged"] += 1
            else:
                r["action"] = "update"
                r["existing_id"] = ex["id"]
                r["price_changed"] = not price_same
                r["desc_changed"]  = desc_diff
                if not price_same:
                    result["price_changed"] += 1
                if desc_diff:
                    result["desc_changed"] += 1
        else:
            r["action"] = "create"
            result["new"] += 1
        token_rows.append(r)

    # Fix 3: would_delete preview
    if delete_missing:
        all_file_norms = {r["norm_name"] for r in token_rows}
        would_del_names = [
            s_name for s_norm, s_name in (
                (_normalize_name(s.name), s.name)
                for s in LabService.query.filter_by(
                    laboratory_id=laboratory_id, city_id=city_id
                ).all()
            ) if s_norm not in all_file_norms
        ]
        result["would_delete"] = {
            "count": len(would_del_names),
            "names": would_del_names[:20],
        }

    token = uuid.uuid4().hex
    _save_preview(token, {
        "laboratory_id": laboratory_id,
        "city_id": city_id,
        "rows": token_rows,
        "existing_norm_names": list(existing.keys()),
    })
    result["token"] = token
    return result


# ---------------------------------------------------------------------------
# Public: commit
# ---------------------------------------------------------------------------
def commit_import(
    token: str,
    city_id: int,
    delete_missing: bool = False,
    generate_ai: bool = True,
) -> dict:
    """
    Execute the DB changes for the previewed import, then kick off background AI.

    Returns:
        {
            "created":       int,
            "updated":       int,
            "deleted":       int,
            "total_for_ai":  int,
            "token":         str,  # same token; use for progress polling
            "error":         str | None,
        }
    """
    result = {"created": 0, "updated": 0, "deleted": 0, "total_for_ai": 0,
              "token": token, "error": None}

    preview = _load_preview(token)
    if not preview:
        result["error"] = "رمز المعاينة منتهي الصلاحية أو غير موجود"; return result

    laboratory_id = preview["laboratory_id"]
    if preview["city_id"] != city_id:
        result["error"] = "city_id لا يتطابق مع المعاينة المحفوظة"; return result

    rows = preview["rows"]
    existing_norm_names: set[str] = set(preview.get("existing_norm_names", []))
    file_norm_names: set[str] = {r["norm_name"] for r in rows if r["action"] != "unchanged"}
    # also add unchanged rows to file_norm_names so they're not deleted
    all_file_norm_names: set[str] = {r["norm_name"] for r in rows}

    created_ids: list[int] = []
    updated_ids: list[int] = []

    try:
        for r in rows:
            action = r["action"]
            if action == "unchanged":
                continue

            if action == "create":
                new_test = LabService(
                    laboratory_id=laboratory_id,
                    city_id=city_id,
                    name=r["name"],
                    price=r["price"],
                    description=r.get("description"),
                    duration=r.get("duration"),
                    sample_type=r.get("sample_type"),
                    sync_status="pending",  # will be set to 'ok' after embedding
                )
                db.session.add(new_test)
                db.session.flush()
                created_ids.append(new_test.id)
                result["created"] += 1

            elif action == "update":
                existing_id = r.get("existing_id")
                if not existing_id:
                    continue
                test = db.session.get(LabService, existing_id)
                if not test:
                    continue
                test.price = r["price"]
                # Fix 1: only update description when it actually differs (flag set in preview)
                if r.get("desc_changed") and r.get("description"):
                    test.description = r["description"]
                    test.sync_status = "pending"   # description changed → re-embed
                # duration / sample_type: update if provided (non-semantic, no re-embed needed)
                if r.get("duration") is not None:
                    test.duration = r["duration"]
                if r.get("sample_type"):
                    test.sample_type = r["sample_type"]
                updated_ids.append(existing_id)
                result["updated"] += 1

        # delete_missing: remove city tests NOT in the file
        deleted_pairs: list[tuple[int, int, int]] = []  # (test_id, lab_id, city_id)
        if delete_missing:
            all_tests = LabService.query.filter_by(
                laboratory_id=laboratory_id, city_id=city_id
            ).all()
            for t in all_tests:
                if _normalize_name(t.name) not in all_file_norm_names:
                    deleted_pairs.append((t.id, t.laboratory_id, t.city_id))
                    db.session.delete(t)
                    result["deleted"] += 1

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        logger.exception("Commit import failed: %s", e)
        result["error"] = f"فشل في الحفظ: {e}"
        return result

    # Delete Qdrant vectors for deleted tests (after commit, non-fatal)
    for test_id, lab_id, c_id in deleted_pairs:
        try:
            delete_test_vector(test_id, laboratory_id=lab_id, city_id=c_id)
        except Exception:
            logger.exception("Failed to delete Qdrant point for deleted test %s", test_id)

    # IDs that need AI or at least vector upsert:
    # new tests always need AI + embedding;
    # updated tests need re-embedding only if description changed (sync_status=pending)
    needs_embedding_ids = created_ids[:]
    for uid in updated_ids:
        t = db.session.get(LabService, uid)
        if t and t.sync_status == "pending":
            needs_embedding_ids.append(uid)

    result["total_for_ai"] = len(needs_embedding_ids)

    if generate_ai and needs_embedding_ids:
        _init_progress(token, len(needs_embedding_ids))
        # Run in a background thread using the Flask app context
        from app import create_app
        app = create_app()
        thread = threading.Thread(
            target=_background_ai_job,
            args=(app, token, laboratory_id, city_id, needs_embedding_ids),
            daemon=True,
        )
        thread.start()
    else:
        _init_progress(token, 0)
        _finish_progress(token)

    return result


# ---------------------------------------------------------------------------
# Background AI + embedding job
# ---------------------------------------------------------------------------
def _background_ai_job(
    app,
    token: str,
    laboratory_id: int,
    city_id: int,
    test_ids: list[int],
) -> None:
    """Runs in a daemon thread. Generates AI fields and upserts vectors."""
    with app.app_context():
        batch_items = []
        failed_ids = []

        for test_id in test_ids:
            test = db.session.get(LabService, test_id)
            if not test:
                _update_progress(token, failed=1)
                continue

            ai_ok = True

            # Generate ONLY missing fields.
            # Fix 5: description from "Test Details" column is NEVER overwritten.
            # Each `if not test.X` guard ensures we only fill empty fields.
            needs_ai = not test.description or not test.sample_type or not test.duration
            if needs_ai:
                try:
                    gen = generate_test(
                        name=test.name,
                        description=test.description,   # passed in so AI knows it exists
                        instructions=test.patient_instructions,
                    )
                    if not test.description:            # Fix 5: ONLY fill if empty
                        test.description = gen.description or None
                    if not test.sample_type:
                        test.sample_type = gen.sample_type or None
                    if not test.duration:
                        test.duration = gen.duration or None
                    if not test.patient_instructions:
                        test.patient_instructions = gen.patient_instructions or None
                    if not test.keywords:
                        test.keywords = gen.keywords or None
                    if not test.alias_name:
                        test.alias_name = gen.alias_name or None
                    time.sleep(AI_RATE_LIMIT_DELAY)
                except Exception as gen_err:
                    logger.warning("AI generation failed for test %s: %s", test_id, gen_err)
                    ai_ok = False

            try:
                db.session.commit()
            except Exception as db_err:
                db.session.rollback()
                logger.exception("Failed to save AI fields for test %s: %s", test_id, db_err)
                ai_ok = False

            batch_items.append({
                "test_id": test_id,
                "test_name": test.name,
                "description": test.description or "",
                "keywords": test.keywords or [],
                "_ai_ok": ai_ok,
            })

        # Batch upsert vectors in chunks of 100
        CHUNK = 100
        for i in range(0, len(batch_items), CHUNK):
            chunk = batch_items[i:i + CHUNK]
            upsert_input = [
                {"test_id": item["test_id"], "test_name": item["test_name"],
                 "description": item["description"], "keywords": item["keywords"]}
                for item in chunk
            ]
            try:
                vec_result = upsert_test_vectors_batch(laboratory_id, city_id, upsert_input)
                succeeded_ids = set(vec_result.get("succeeded", []))
                failed_vec = {f["test_id"] for f in vec_result.get("failed", [])}
            except Exception as vec_err:
                logger.exception("Batch vector upsert failed for chunk at %d: %s", i, vec_err)
                succeeded_ids = set()
                failed_vec = {item["test_id"] for item in chunk}

            for item in chunk:
                tid = item["test_id"]
                t = db.session.get(LabService, tid)
                if not t:
                    continue
                if tid in succeeded_ids:
                    t.sync_status = "ok"
                    _update_progress(token, done=1)
                else:
                    t.sync_status = "pending"
                    failed_ids.append(tid)
                    _update_progress(token, failed=1)

            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

        _finish_progress(token)
        logger.info(
            "Background AI job done: token=%s lab=%s city=%s succeeded=%d failed=%d",
            token, laboratory_id, city_id,
            len(test_ids) - len(failed_ids), len(failed_ids)
        )


# ---------------------------------------------------------------------------
# Public: retry_pending
# ---------------------------------------------------------------------------
def retry_pending(laboratory_id: int, city_id: int) -> dict:
    """
    Re-run AI generation and vector upsert for all sync_status='pending'
    tests in the given city.

    Returns {"queued": int} — the number of tests submitted to background.
    """
    pending_tests = LabService.query.filter_by(
        laboratory_id=laboratory_id,
        city_id=city_id,
        sync_status="pending",
    ).all()

    if not pending_tests:
        return {"queued": 0}

    test_ids = [t.id for t in pending_tests]
    token = f"retry_{uuid.uuid4().hex}"

    from app import create_app
    app = create_app()
    _init_progress(token, len(test_ids))

    thread = threading.Thread(
        target=_background_ai_job,
        args=(app, token, laboratory_id, city_id, test_ids),
        daemon=True,
    )
    thread.start()

    logger.info("retry_pending started: lab=%s city=%s count=%d token=%s",
                laboratory_id, city_id, len(test_ids), token)

    return {"queued": len(test_ids), "token": token}
