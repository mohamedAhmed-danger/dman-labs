"""
price_import_service.py

Two-phase import: preview -> commit (Excel / CSV price lists per city).

Required columns : "Test Name" | "اسم التحليل" | "name"   +   "Price" | "السعر" | "price"
Optional columns : Test Details | Duration | Sample Type  (Arabic aliases supported)
"""
import csv
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from io import BytesIO
from typing import IO

from flask import current_app

from models.models import db, LabService, City
from services.shared.generation_service import generate_test
from services.shared.vector_service import (
    upsert_test_vectors_batch,
    delete_test_vector,
)
from services.messaging.redis_queue import get_redis_client

logger = logging.getLogger(__name__)

AI_RATE_LIMIT_DELAY = float(os.getenv("AI_RATE_LIMIT_DELAY", "1"))
PREVIEW_TTL_SECONDS = 1800
MAX_IMPORT_FILE_SIZE = 16 * 1024 * 1024
MAX_DELETE_RATIO = 0.5      # refuse delete_missing if it would delete more than this share
JOB_CHUNK = 20              # كل كام تحليل نرفع vectors ونحدّث الشريط

_NAME_ALIASES = {"test name", "اسم التحليل", "name", "الاسم"}
_PRICE_ALIASES = {"price", "السعر", "سعر التحليل"}
_DESC_ALIASES = {"test details", "تفاصيل التحليل", "description", "وصف", "الوصف"}
_DURATION_ALIASES = {"duration", "وقت الاستلام", "مدة التحليل", "المدة", "hours"}
_SAMPLE_ALIASES = {"sample type", "نوع العينة", "sample_type", "العينة"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _s(v):
    return v.decode("utf-8") if isinstance(v, bytes) else v


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _normalize_name(raw: str) -> str:
    return _norm_ws(raw).lower()


def _cell_str(val) -> str:
    return str(val).strip() if val is not None else ""


_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


def _cell_float(val, allow_zero: bool = False) -> float | None:
    """Parse a price. Returns None if invalid (negative, empty, text)."""
    if val is None:
        return None
    s = str(val).strip().translate(_ARABIC_INDIC)
    if not s or s.startswith("-"):
        return None
    s = re.sub(r"[^\d.]", "", s)   # drops commas, spaces, currency text
    if not s:
        return None
    try:
        result = float(s)
    except ValueError:
        return None
    return result if (result > 0 or allow_zero) else None


def _cell_int(val) -> int | None:
    f = _cell_float(val)
    return int(f) if f else None


def _get_cell(row, col_map, field):
    idx = col_map.get(field)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _map_headers(headers: list[str]) -> dict:
    groups = {
        "name": _NAME_ALIASES, "price": _PRICE_ALIASES, "description": _DESC_ALIASES,
        "duration": _DURATION_ALIASES, "sample_type": _SAMPLE_ALIASES,
    }
    mapping = {k: None for k in groups}
    for idx, h in enumerate(headers):
        hl = _norm_ws(h).lower()
        for field, aliases in groups.items():
            if mapping[field] is None and hl in {a.lower() for a in aliases}:
                mapping[field] = idx
                break
    return mapping


def _desc_changed(new_desc, db_desc) -> bool:
    if not new_desc:
        return False
    return _norm_ws(new_desc) != _norm_ws(db_desc or "")


def _compute_changes(r: dict, cur_price, cur_desc, cur_duration, cur_sample) -> dict:
    return {
        "price": abs(float(cur_price or 0) - r["price"]) >= 0.001,
        "desc": _desc_changed(r.get("description"), cur_desc),
        "duration": r.get("duration") is not None and r["duration"] != cur_duration,
        "sample": bool(r.get("sample_type")) and _norm_ws(r["sample_type"]) != _norm_ws(cur_sample or ""),
    }


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------
def _read_excel(file_obj: IO[bytes]):
    import openpyxl
    wb = openpyxl.load_workbook(BytesIO(file_obj.read()), data_only=True, read_only=True)
    try:
        rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
    finally:
        wb.close()
    if not rows:
        return [], []
    return [_cell_str(c) for c in rows[0]], rows[1:]


def _read_xls(file_obj: IO[bytes]):
    import xlrd
    sheet = xlrd.open_workbook(file_contents=file_obj.read()).sheet_by_index(0)
    if sheet.nrows == 0:
        return [], []
    return [_cell_str(v) for v in sheet.row_values(0)], [sheet.row_values(i) for i in range(1, sheet.nrows)]


def _read_csv(file_obj: IO[bytes]):
    content = file_obj.read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1256"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("تعذّر قراءة ملف CSV — تأكد من Encoding")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [], []
    return [c.strip() for c in rows[0]], rows[1:]


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------
def _preview_key(token): return f"import:preview:{token}"
def _progress_key(token): return f"import:progress:{token}"
def _city_progress_key(city_id): return f"import:city:{city_id}:latest"
def _lock_key(token): return f"import:lock:{token}"


def _save_preview(token: str, data: dict) -> None:
    get_redis_client().set(_preview_key(token), json.dumps(data, ensure_ascii=False), ex=PREVIEW_TTL_SECONDS)


def _load_preview(token: str) -> dict | None:
    raw = get_redis_client().get(_preview_key(token))
    return json.loads(_s(raw)) if raw else None


def _init_progress(token: str, total: int) -> None:
    r = get_redis_client()
    r.hset(_progress_key(token), mapping={
        "total": total, "done": 0, "failed": 0, "pending": total, "status": "running"
    })
    r.expire(_progress_key(token), PREVIEW_TTL_SECONDS)


def _update_progress(token: str, done: int = 0, failed: int = 0) -> None:
    r = get_redis_client()
    if done:
        r.hincrby(_progress_key(token), "done", done)
    if failed:
        r.hincrby(_progress_key(token), "failed", failed)
    if done or failed:
        r.hincrby(_progress_key(token), "pending", -(done + failed))


def _finish_progress(token: str) -> None:
    r = get_redis_client()
    r.hset(_progress_key(token), "status", "done")
    r.hset(_progress_key(token), "pending", 0)


def get_import_progress(token: str) -> dict:
    data = get_redis_client().hgetall(_progress_key(token))
    if not data:
        return {}
    out = {}
    for k, v in data.items():
        k, v = _s(k), _s(v)
        out[k] = int(v) if v.lstrip("-").isdigit() else v
    return out


def get_city_import_progress(city_id: int) -> dict:
    token = _s(get_redis_client().get(_city_progress_key(city_id)))
    return get_import_progress(token) if token else {}


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
def preview_import(file_obj, filename, laboratory_id, city_id, delete_missing=False) -> dict:
    result = {"token": None, "new": 0, "updated": 0, "price_changed": 0, "desc_changed": 0,
              "unchanged": 0, "needs_review": [], "warnings": [],
              "would_delete": {"count": 0, "names": []}, "error": None}

    city = db.session.get(City, city_id)
    if not city:
        result["error"] = "المدينة غير موجودة"
        return result
    if city.laboratory_id != laboratory_id:
        result["error"] = "المدينة لا تنتمي إلى هذا المعمل"
        return result

    ext = (filename or "").rsplit(".", 1)[-1].lower()
    try:
        pos = file_obj.tell()
        file_obj.seek(0, 2)
        size = file_obj.tell()
        file_obj.seek(pos)
    except (AttributeError, OSError):
        size = 0
    if size > MAX_IMPORT_FILE_SIZE:
        result["error"] = "حجم الملف أكبر من الحد المسموح (16 ميجابايت)"
        return result

    try:
        if ext == "xlsx":
            headers, rows = _read_excel(file_obj)
        elif ext == "xls":
            headers, rows = _read_xls(file_obj)
        elif ext == "csv":
            headers, rows = _read_csv(file_obj)
        else:
            result["error"] = f"نوع الملف '{ext}' غير مدعوم. استخدم xlsx أو xls أو csv."
            return result
    except Exception as e:
        result["error"] = f"خطأ في قراءة الملف: {e}"
        return result

    if not headers or not rows:
        result["error"] = "الملف فارغ"
        return result

    col_map = _map_headers(headers)
    if col_map["name"] is None:
        result["error"] = "لم يتم العثور على عمود 'Test Name' (أو 'اسم التحليل'). تأكد من عنوان العمود."
        return result
    if col_map["price"] is None:
        result["error"] = "لم يتم العثور على عمود 'Price' (أو 'السعر'). تأكد من عنوان العمود."
        return result

    all_tests = LabService.query.filter_by(laboratory_id=laboratory_id, city_id=city_id).all()
    existing = {
        _normalize_name(s.name): {
            "price": s.price, "description": s.description,
            "duration": s.duration, "sample_type": s.sample_type, "name": s.name,
        } for s in all_tests
    }

    parsed: dict[str, dict] = {}          # norm_name -> row (last wins)
    skipped_norms: set[str] = set()       # rows rejected but whose test must NOT be deleted

    for row_idx, row in enumerate(rows, start=2):
        raw_name = _cell_str(_get_cell(row, col_map, "name"))
        if not raw_name:
            if any(_cell_str(v) for v in row):
                result["needs_review"].append({"row": row_idx, "name": "", "reason": "اسم التحليل فارغ"})
            continue

        norm = _normalize_name(raw_name)

        if len(raw_name) > 255:
            skipped_norms.add(norm)
            result["needs_review"].append({"row": row_idx, "name": raw_name[:60] + "…",
                                           "reason": "اسم التحليل أطول من 255 حرفاً"})
            continue

        price_cell = _get_cell(row, col_map, "price")
        price = _cell_float(price_cell, allow_zero=True)
        if price is None:
            skipped_norms.add(norm)
            shown = _cell_str(price_cell) or "—"
            result["needs_review"].append({"row": row_idx, "name": raw_name,
                                           "reason": f"سعر غير صالح: '{shown}'"})
            continue

        if norm in parsed:
            result["warnings"].append(
                f"اسم مكرر في الملف: '{raw_name}' (صف {parsed[norm]['row']} و{row_idx}) — سيُستخدم آخر صف")

        parsed[norm] = {
            "row": row_idx,
            "name": _norm_ws(raw_name),
            "norm_name": norm,
            "price": price,
            "description": _cell_str(_get_cell(row, col_map, "description")) or None,
            "duration": _cell_int(_get_cell(row, col_map, "duration")),
            "sample_type": _cell_str(_get_cell(row, col_map, "sample_type")) or None,
        }

    for norm, r in parsed.items():
        ex = existing.get(norm)
        if ex is None:
            result["new"] += 1
            continue
        ch = _compute_changes(r, ex["price"], ex["description"], ex["duration"], ex["sample_type"])
        if not any(ch.values()):
            result["unchanged"] += 1
        else:
            result["updated"] += 1
            result["price_changed"] += ch["price"]
            result["desc_changed"] += ch["desc"]

    if delete_missing:
        protected = set(parsed) | skipped_norms
        names = [t.name for t in all_tests if _normalize_name(t.name) not in protected]
        result["would_delete"] = {"count": len(names), "names": names[:10]}
        if all_tests and len(all_tests) >= 10 and len(names) > MAX_DELETE_RATIO * len(all_tests):
            result["warnings"].append(
                f"الحذف هيمسح {len(names)} من {len(all_tests)} تحليل — العملية هتترفض عند التنفيذ. راجع الملف.")

    token = uuid.uuid4().hex
    _save_preview(token, {
        "laboratory_id": laboratory_id,
        "city_id": city_id,
        "rows": list(parsed.values()),
        "skipped_norm_names": sorted(skipped_norms),
    })
    result["token"] = token
    logger.info("Import preview: lab=%s city=%s file=%s new=%d updated=%d unchanged=%d review=%d token=%s",
                laboratory_id, city_id, filename, result["new"], result["updated"],
                result["unchanged"], len(result["needs_review"]), token)
    return result


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------
def commit_import(token: str, city_id: int, delete_missing: bool = False, generate_ai: bool = True) -> dict:
    result = {"created": 0, "updated": 0, "deleted": 0, "total_for_ai": 0, "token": token, "error": None}

    preview = _load_preview(token)
    if not preview:
        result["error"] = "رمز المعاينة منتهي الصلاحية أو تم استخدامه بالفعل"
        return result
    if preview["city_id"] != city_id:
        result["error"] = "city_id لا يتطابق مع المعاينة المحفوظة"
        return result

    laboratory_id = preview["laboratory_id"]
    rows = preview["rows"]
    protected = {r["norm_name"] for r in rows} | set(preview.get("skipped_norm_names", []))

    redis = get_redis_client()
    if not redis.set(_lock_key(token), 1, nx=True, ex=600):
        result["error"] = "عملية الاستيراد قيد التنفيذ بالفعل"
        return result

    created_tests: list[LabService] = []
    reembed_ids: list[int] = []
    deleted_pairs: list[tuple[int, int, int]] = []

    try:
        all_tests = LabService.query.filter_by(laboratory_id=laboratory_id, city_id=city_id).all()
        existing = {_normalize_name(t.name): t for t in all_tests}

        to_delete = []
        if delete_missing:
            to_delete = [t for t in all_tests if _normalize_name(t.name) not in protected]
            if len(all_tests) >= 10 and len(to_delete) > MAX_DELETE_RATIO * len(all_tests):
                redis.delete(_lock_key(token))
                result["error"] = (f"الحذف هيمسح {len(to_delete)} من {len(all_tests)} تحليل "
                                   f"(أكتر من {int(MAX_DELETE_RATIO * 100)}%). راجع الملف.")
                return result

        for r in rows:
            t = existing.get(r["norm_name"])
            if t is None:
                new_test = LabService(
                    laboratory_id=laboratory_id, city_id=city_id,
                    name=r["name"], price=r["price"], description=r.get("description"),
                    duration=r.get("duration"), sample_type=r.get("sample_type"),
                    sync_status="pending",
                )
                db.session.add(new_test)
                created_tests.append(new_test)
                continue

            ch = _compute_changes(r, t.price, t.description, t.duration, t.sample_type)
            if not any(ch.values()):
                continue
            if ch["price"]:
                t.price = r["price"]
            if ch["desc"]:
                t.description = r["description"]
                t.sync_status = "pending"
                reembed_ids.append(t.id)
            if ch["duration"]:
                t.duration = r["duration"]
            if ch["sample"]:
                t.sample_type = r["sample_type"]
            result["updated"] += 1

        for t in to_delete:
            deleted_pairs.append((t.id, t.laboratory_id, t.city_id))
            db.session.delete(t)
        result["deleted"] = len(to_delete)

        db.session.flush()   # assigns ids to new rows
        result["created"] = len(created_tests)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        redis.delete(_lock_key(token))
        logger.exception("Commit import failed: %s", e)
        result["error"] = f"فشل في الحفظ (لو فيه حجوزات أو باقات مربوطة بتحاليل محذوفة هيتمنع الحذف): {e}"
        result["created"] = result["updated"] = result["deleted"] = 0
        return result

    redis.delete(_preview_key(token))   # token can't be committed twice
    logger.info("Import commit: token=%s city=%s created=%d updated=%d deleted=%d for_ai=%d",
                token, city_id, result["created"], result["updated"], result["deleted"],
                len(created_tests) + len(reembed_ids))

    for test_id, lab_id, c_id in deleted_pairs:
        try:
            delete_test_vector(test_id, laboratory_id=lab_id, city_id=c_id)
        except Exception:
            logger.exception("Failed to delete Qdrant point for test %s", test_id)

    needs_embedding_ids = [t.id for t in created_tests] + reembed_ids
    result["total_for_ai"] = len(needs_embedding_ids)

    _init_progress(token, len(needs_embedding_ids))
    redis.set(_city_progress_key(city_id), token, ex=PREVIEW_TTL_SECONDS)

    if needs_embedding_ids:
        app = current_app._get_current_object()
        threading.Thread(
            target=_background_job,
            args=(app, token, laboratory_id, city_id, needs_embedding_ids, generate_ai),
            daemon=True,
        ).start()
    else:
        _finish_progress(token)

    return result


# ---------------------------------------------------------------------------
# Background job: AI fill (optional) + vector upsert
# ---------------------------------------------------------------------------
def _background_job(app, token, laboratory_id, city_id, test_ids, generate_ai=True) -> None:
    try:
        with app.app_context():
            _run_job(token, laboratory_id, city_id, test_ids, generate_ai)
    except Exception:
        logger.exception("Background import job crashed: token=%s", token)
    finally:
        _finish_progress(token)


def _flush_chunk(token, laboratory_id, city_id, chunk) -> int:
    """يرفع vectors لدفعة، يحدّث sync_status والـ progress. يرجّع عدد الناجح."""
    if not chunk:
        return 0

    payload = [{"test_id": it["test_id"], "test_name": it["test_name"],
                "description": it["description"], "keywords": it["keywords"]} for it in chunk]
    try:
        vec = upsert_test_vectors_batch(laboratory_id, city_id, payload)
        succeeded = set(vec.get("succeeded", []))
    except Exception:
        logger.exception("Batch vector upsert failed: token=%s", token)
        succeeded = set()

    ok = 0
    for it in chunk:
        t = db.session.get(LabService, it["test_id"])
        if not t:
            _update_progress(token, failed=1)
            continue
        if it["test_id"] in succeeded and it["ai_ok"]:
            t.sync_status = "ok"
            ok += 1
            _update_progress(token, done=1)
        else:
            t.sync_status = "pending"
            _update_progress(token, failed=1)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to save sync_status: token=%s", token)
    return ok


def _run_job(token, laboratory_id, city_id, test_ids, generate_ai) -> None:
    total = len(test_ids)
    chunk = []
    ok_count = 0
    logger.info("Import job started: token=%s total=%d generate_ai=%s", token, total, generate_ai)

    for n, test_id in enumerate(test_ids, start=1):
        test = db.session.get(LabService, test_id)
        if not test:
            _update_progress(token, failed=1)
            continue

        ai_ok = True
        if generate_ai and (not test.description or not test.sample_type or not test.duration):
            try:
                gen = generate_test(
                    name=test.name,
                    description=test.description,
                    instructions=test.patient_instructions,
                )
                # only fill EMPTY fields — never overwrite what came from the file
                if not test.description:
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
            except Exception as err:
                logger.warning("AI generation failed for test %s: %s", test_id, err)
                ai_ok = False
            finally:
                time.sleep(AI_RATE_LIMIT_DELAY)

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Failed to save AI fields for test %s", test_id)
            ai_ok = False

        chunk.append({
            "test_id": test_id,
            "test_name": test.name,
            "description": test.description or "",
            "keywords": test.keywords or [],
            "ai_ok": ai_ok,
        })

        if len(chunk) >= JOB_CHUNK or n == total:
            ok_count += _flush_chunk(token, laboratory_id, city_id, chunk)
            chunk = []
            logger.info("Import progress: token=%s %d/%d processed (ok so far=%d)",
                        token, n, total, ok_count)

    ok_count += _flush_chunk(token, laboratory_id, city_id, chunk)   # أي باقي
    logger.info("Import job done: token=%s lab=%s city=%s ok=%d failed=%d",
                token, laboratory_id, city_id, ok_count, total - ok_count)


# ---------------------------------------------------------------------------
# Retry pending
# ---------------------------------------------------------------------------
def retry_pending(laboratory_id: int, city_id: int) -> dict:
    ids = [t.id for t in LabService.query.filter_by(
        laboratory_id=laboratory_id, city_id=city_id, sync_status="pending").all()]
    if not ids:
        return {"queued": 0}

    token = f"retry_{uuid.uuid4().hex}"
    _init_progress(token, len(ids))
    get_redis_client().set(_city_progress_key(city_id), token, ex=PREVIEW_TTL_SECONDS)

    app = current_app._get_current_object()
    threading.Thread(
        target=_background_job,
        args=(app, token, laboratory_id, city_id, ids, True),
        daemon=True,
    ).start()
    return {"queued": len(ids), "token": token}