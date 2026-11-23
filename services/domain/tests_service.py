import logging

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from models.models import db, LabService, LabServiceCityPrice, Laboratory, City
from services.shared.vector_service import upsert_test_vector, delete_test_vector
from services.shared.generation_service import regenerate_test, generate_test
from schemas.generation import TestGenerationResult

logger = logging.getLogger(__name__)

DUPLICATE_TEST_MSG = "التحليل موجود بالفعل في هذا المعمل"
NOT_FOUND_MSG = "Test not found"
INVALID_PRICE_MSG = "سعر التحليل غير صحيح"


def _available_in_city(city_id: int):
    """SQL condition: the test is available in this city.
    Available = it has a price row for the city OR it has a default price."""
    return or_(
        LabService.price.isnot(None),
        LabService.city_prices.any(LabServiceCityPrice.city_id == city_id),
    )


def _parse_price(raw):
    """Returns (value, error). Empty input -> (None, None)."""
    if raw is None or str(raw).strip() == "":
        return None, None
    try:
        val = float(raw)
    except (ValueError, TypeError):
        return None, "السعر يجب أن يكون رقماً صحيحاً"
    if val < 0:
        return None, INVALID_PRICE_MSG
    return val, None


def _collect_city_prices(form_data) -> dict:
    """Collect every form field named city_price_<id> into {city_id: raw_value}.
    Empty values are kept (they mean 'clear this city's price')."""
    prices = {}
    for key in form_data.keys():
        if key.startswith("city_price_"):
            suffix = key[len("city_price_"):]
            if suffix.isdigit():
                prices[int(suffix)] = form_data.get(key)
    return prices


def _validate_city_prices(prices: dict, laboratory_id: int):
    """Validate {city_id: raw} against the laboratory's cities.
    Returns ({city_id: float | None}, error). None means 'no price / delete'."""
    if not prices:
        return {}, None
    valid_ids = {
        c.id for c in City.query.filter_by(laboratory_id=laboratory_id)
        .with_entities(City.id).all()
    }
    parsed = {}
    for cid, raw in prices.items():
        try:
            cid = int(cid)
        except (ValueError, TypeError):
            return None, "معرّف المدينة غير صحيح"
        if cid not in valid_ids:
            return None, "المدينة لا تنتمي إلى هذا المعمل"
        val, err = _parse_price(raw)
        if err:
            return None, err
        parsed[cid] = val
    return parsed, None


def _apply_city_prices(test: LabService, parsed: dict):
    """Add / update / delete city price rows on a test (no commit)."""
    existing = {cp.city_id: cp for cp in test.city_prices}
    for cid, val in parsed.items():
        row = existing.get(cid)
        if val is None:
            if row:
                test.city_prices.remove(row)  # delete-orphan removes the row
        elif row:
            row.price = val
        else:
            test.city_prices.append(LabServiceCityPrice(city_id=cid, price=val))


def _parse_tags(raw_list, fallback_str=None):
    """
    Parses and sanitizes list of tags/keywords/aliases from form data.
    Handles both multi-element lists and comma-separated string tokens.
    """
    tags = []
    if raw_list:
        for item in raw_list:
            if isinstance(item, str):
                for part in item.split(","):
                    p = part.strip()
                    if p and p not in tags:
                        tags.append(p)
            elif isinstance(item, (list, tuple)):
                for sub in item:
                    p = str(sub).strip()
                    if p and p not in tags:
                        tags.append(p)
    if not tags and fallback_str and isinstance(fallback_str, str):
        tags = [p.strip() for p in fallback_str.split(",") if p.strip()]
    return tags


class TestsService:
    # city_id here is only the "selected city for display/pricing", not a property of the test.
    def __init__(self, test_id=None, test=None, laboratory_id=None, city_id=None):
        """Init with a test_id, a pre-loaded LabService, or laboratory_id/city_id.
        If `test` is given, test_id/laboratory_id are derived from it."""
        self.test_id = test_id
        self._test = test
        self.laboratory_id = laboratory_id
        self.city_id = city_id
        if test:
            self.test_id = test.id
            if getattr(test, 'laboratory_id', None) is not None:
                self.laboratory_id = test.laboratory_id

    @property
    def test(self):
        """Lazily load and cache the LabService for self.test_id."""
        if self._test is None and self.test_id is not None:
            self._test = db.session.get(LabService, self.test_id)
            if self._test and self.laboratory_id is None:
                self.laboratory_id = self._test.laboratory_id
        return self._test

    def get_test(self, test_id=None):
        """Fetch a test by ID (or self.test_id), using the instance cache when it matches. Returns (LabService or None, message)."""
        target_id = test_id or self.test_id
        if not target_id and self._test:
            return self._test, "Test retrieved successfully"
        if not target_id:
            return None, "Test not found"

        if self._test and self._test.id == target_id:
            return self._test, "Test retrieved successfully"

        try:
            test = db.session.get(LabService, target_id)
            if test:
                if target_id == self.test_id or self.test_id is None:
                    self._test = test
                    self.test_id = test.id
                    if self.laboratory_id is None:
                        self.laboratory_id = test.laboratory_id
                return test, "Test retrieved successfully"
            else:
                return None, "Test not found"
        except Exception as e:
            logger.exception("Failed to fetch test %s: %s", target_id, e)
            return None, "حدث خطأ أثناء جلب بيانات التحليل"

    def get_test_by_id(self, test_id=None):
        """Backward-compatible alias for get_test()."""
        return self.get_test(test_id=test_id)

    def get_paginated_tests(self, page: int = 1, per_page: int = 10, search: str = None,
                            laboratory_id: int = None, city_id: int = None, sync_status: str = None):
        """Paginated LabService list, optionally filtered by name (ILIKE), laboratory_id, sync_status
        and city (tests available in that city). Ordered ascending by id.
        City prices are eager-loaded to avoid one query per row."""
        lab_id = laboratory_id or self.laboratory_id
        c_id = city_id or self.city_id
        try:
            query = LabService.query.options(selectinload(LabService.city_prices))
            if search and search.strip():
                search_term = f"%{search.strip()}%"
                query = query.filter(LabService.name.ilike(search_term))
            if lab_id:
                query = query.filter(LabService.laboratory_id == lab_id)
            if c_id:
                query = query.filter(_available_in_city(c_id))
            if sync_status:
                query = query.filter(LabService.sync_status == sync_status)

            pagination = query.order_by(LabService.id.asc()).paginate(
                page=page,
                per_page=per_page,
                error_out=False
            )
            return pagination, "Tests retrieved successfully"
        except Exception as e:
            logger.exception("Failed to paginate tests: %s", e)
            return None, "حدث خطأ أثناء جلب قائمة التحاليل"

    def search_services(self, query: str = "", limit: int = 15, laboratory_id: int = None, city_id: int = None):
        """Autocomplete search over name/aliases/keywords, ranked by match priority.
        Returns a list of dicts (id, name, price, patient_instructions), or [] on no query or error.
        The price is the city price when city_id is given, otherwise the default price."""
        if not query or not query.strip():
            return []

        clean_query = query.strip()
        search_term = f"%{clean_query}%"
        lab_id = laboratory_id or self.laboratory_id
        c_id = city_id or self.city_id

        try:
            from sqlalchemy import cast, String, case

            query_filter = or_(
                LabService.name.ilike(search_term),
                cast(LabService.alias_name, String).ilike(search_term),
                cast(LabService.keywords, String).ilike(search_term),
            )

            stmt = LabService.query.options(selectinload(LabService.city_prices)).filter(query_filter)
            if lab_id:
                stmt = stmt.filter(LabService.laboratory_id == lab_id)
            if c_id:
                stmt = stmt.filter(_available_in_city(c_id))

            priority = case(
                (LabService.name.ilike(clean_query), 0),
                (LabService.name.ilike(f"{clean_query}%"), 1),
                (cast(LabService.alias_name, String).ilike(search_term), 2),
                else_=3
            )

            services = stmt.order_by(priority, LabService.name.asc()).limit(limit).all()

            results = []
            for s in services:
                price = s.get_price_for_city(c_id) if c_id else s.price
                results.append({
                    "id": s.id,
                    "name": s.name,
                    "price": float(price) if price is not None else None,
                    "patient_instructions": (s.patient_instructions[:80] + "...") if s.patient_instructions and len(s.patient_instructions) > 80 else (s.patient_instructions or "")
                })
            return results
        except Exception as e:
            logger.exception("search_services failed for query=%r: %s", clean_query, e)
            return []

    def create_lab_service(self, laboratory_id: int = None, name: str = None,
                           price: float = None, description: str = None,
                           patient_instructions: str = None, duration: int = None,
                           sample_type: str = None, keywords: list[str] = None,
                           alias_name: list[str] = None, city_prices: dict = None):
        """Create a LabService (once per laboratory) and index it in Qdrant.
        price        : optional default price (used for cities without their own price).
        city_prices  : optional {city_id: price}; empty values are skipped.
        Duplicate names (per laboratory) are rejected via a pre-check plus an IntegrityError catch.
        Returns (LabService or None, message).
        """
        lab_id = laboratory_id or self.laboratory_id
        try:
            name = name.strip() if name else ""
            if not name:
                return None, "اسم التحليل مطلوب"
            if not lab_id:
                return None, "معرّف المعمل مطلوب"

            default_price, err = _parse_price(price)
            if err:
                return None, err

            parsed_prices, err = _validate_city_prices(city_prices or {}, lab_id)
            if err:
                return None, err
            parsed_prices = {cid: v for cid, v in parsed_prices.items() if v is not None}

            if default_price is None and not parsed_prices:
                return None, "يجب إدخال سعر افتراضي أو سعر لمدينة واحدة على الأقل"

            if LabService.query.filter_by(laboratory_id=lab_id, name=name).first():
                return None, DUPLICATE_TEST_MSG

            new_test = LabService(
                laboratory_id=lab_id,
                name=name,
                price=default_price,
                description=description,
                patient_instructions=patient_instructions,
                duration=duration,
                sample_type=sample_type,
                keywords=keywords,
                alias_name=alias_name
            )
            for cid, val in parsed_prices.items():
                new_test.city_prices.append(LabServiceCityPrice(city_id=cid, price=val))

            db.session.add(new_test)
            db.session.commit()  # test + city prices saved together
            self._test = new_test
            self.test_id = new_test.id
            self.laboratory_id = new_test.laboratory_id

            try:
                upsert_test_vector(
                    new_test.id, new_test.name,
                    new_test.description or "", new_test.keywords or [],
                    laboratory_id=lab_id
                )
                new_test.sync_status = "ok"
            except Exception as ve:
                logger.warning("Failed to upsert vector for test %s: %s", new_test.id, ve)
                new_test.sync_status = "pending"
                db.session.commit()   # persist the pending status
                return new_test, "تم حفظ التحليل بنجاح ولكن فشل تحديث الفهرس، سيتم إعادة المحاولة لاحقاً"

            db.session.commit()   # persist ok status
            return new_test, "Test created successfully"
        except IntegrityError as e:
            db.session.rollback()
            logger.warning("IntegrityError creating test name=%r lab=%r: %s", name, lab_id, e)
            return None, DUPLICATE_TEST_MSG
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to create test name=%r: %s", name, e)
            return None, "حدث خطأ أثناء إنشاء التحليل"

    def set_city_prices(self, prices: dict, test_id: int = None):
        """
        prices: {city_id: "price"}
        - price given -> add or update the price for that city
        - empty       -> delete that city's price
        Returns (LabService or None, message).
        """
        try:
            target_id = test_id or self.test_id
            test = self._test if (self._test and (not target_id or self._test.id == target_id)) \
                else (db.session.get(LabService, target_id) if target_id else None)
            if not test:
                return None, NOT_FOUND_MSG

            parsed, err = _validate_city_prices(prices or {}, test.laboratory_id)
            if err:
                return None, err

            _apply_city_prices(test, parsed)
            db.session.commit()
            self._test = test
            self.test_id = test.id
            return test, "تم حفظ أسعار المدن بنجاح"
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to set city prices for test %s: %s", test_id, e)
            return None, "حدث خطأ أثناء حفظ أسعار المدن"

    def update_test(self, test_id: int = None, name: str = None, price: float = None, description: str = None,
                    instructions: str = None, duration: int = None, sample_type: str = None,
                    keywords: list[str] = None, alias_name: list[str] = None, laboratory_id: int = None,
                    city_id: int = None, city_prices: dict = None):
        """Update the given non-None fields on a LabService and refresh its vector index.
        price       : the DEFAULT price (None = unchanged).
        city_prices : optional {city_id: price}; empty value deletes that city's price.
        city_id     : deprecated, ignored (tests no longer belong to a city).
        The laboratory of a test cannot be changed (its vector lives in the lab collection).
        Only name/description/keywords/alias_name changes trigger re-embedding.
        Returns (LabService or None, message); None means not found or the update failed.
        """
        target_id = test_id or self.test_id
        try:
            test = None
            if self._test and (not target_id or self._test.id == target_id):
                test = self._test
            elif target_id:
                test = db.session.get(LabService, target_id)

            if not test:
                return None, NOT_FOUND_MSG

            if laboratory_id is not None and laboratory_id != test.laboratory_id:
                return None, "لا يمكن تغيير معمل التحليل بعد إنشائه"

            parsed_prices = None
            if city_prices:
                parsed_prices, err = _validate_city_prices(city_prices, test.laboratory_id)
                if err:
                    return None, err

            if price is not None:
                price_val, err = _parse_price(price)
                if err:
                    return None, err
                test.price = price_val

            if name is not None:
                test.name = name
            if description is not None:
                test.description = description
            if instructions is not None:
                test.patient_instructions = instructions
            if duration is not None:
                test.duration = duration
            if sample_type is not None:
                test.sample_type = sample_type
            if keywords is not None:
                test.keywords = keywords
            if alias_name is not None:
                test.alias_name = alias_name
            if parsed_prices:
                _apply_city_prices(test, parsed_prices)

            db.session.commit()
            self._test = test
            self.test_id = test.id
            self.laboratory_id = test.laboratory_id

            # Only re-embed when semantic fields changed (name / description / keywords / alias)
            semantic_fields_changed = any(f is not None for f in [name, description, keywords, alias_name])
            if semantic_fields_changed:
                try:
                    upsert_test_vector(
                        test.id, test.name,
                        test.description or "", test.keywords or [],
                        laboratory_id=test.laboratory_id
                    )
                    test.sync_status = "ok"
                except Exception as ve:
                    logger.warning("Failed to upsert vector for test %s: %s", test.id, ve)
                    test.sync_status = "pending"
                    db.session.commit()   # persist pending status
                    return test, "تم حفظ التحليل ولكن فشل تحديث الفهرس، سيتم إعادة المحاولة لاحقاً"
                db.session.commit()   # persist ok status

            return test, "Test updated successfully"
        except IntegrityError as e:
            db.session.rollback()
            logger.warning("IntegrityError updating test %s: %s", target_id, e)
            return None, DUPLICATE_TEST_MSG
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to update test %s: %s", target_id, e)
            return None, "حدث خطأ أثناء تحديث بيانات التحليل"

    def delete_test(self, test_id: int = None):
        """Delete a test (its city prices go with it via cascade) and remove its vector from Qdrant.
        Returns (deleted LabService or None, message)."""
        target_id = test_id or self.test_id
        try:
            test = None
            if self._test and (not target_id or self._test.id == target_id):
                test = self._test
            elif target_id:
                test = db.session.get(LabService, target_id)

            if not test:
                return None, NOT_FOUND_MSG

            test_id_copy = test.id
            lab_id_copy = test.laboratory_id
            db.session.delete(test)
            db.session.commit()
            self._test = None

            try:
                delete_test_vector(test_id_copy, laboratory_id=lab_id_copy)
            except Exception as ve:
                logger.warning("Failed to delete vector for test %s: %s", test_id_copy, ve)

            return test, "Test deleted successfully"
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to delete test %s: %s", target_id, e)
            return None, "حدث خطأ أثناء حذف التحليل"

    def get_all_laboratories(self):
        """All laboratories, ordered alphabetically by name."""
        from services.domain.laboratory_service import LaboratoryService
        return LaboratoryService.get_laboratories_ordered_by_name()

    def get_test_service_page_data(self, page=1, per_page=10, search="", laboratory_id=None, city_id=None, sync_status=None):
        """Bundle pagination + services + laboratories for the dashboard view.
        page/per_page are clamped (page>=1, 1<=per_page<=100)."""
        if page < 1:
            page = 1
        if per_page < 1 or per_page > 100:
            per_page = 10

        lab_id = laboratory_id or self.laboratory_id
        c_id = city_id or self.city_id
        pagination, message = self.get_paginated_tests(
            page=page, per_page=per_page, search=search, laboratory_id=lab_id, city_id=c_id, sync_status=sync_status
        )

        services = pagination.items if pagination else []
        laboratories = self.get_all_laboratories()

        return pagination, services, laboratories, message

    def handle_create_test(self, form_data):
        """Validate form_data and create a test.
        Required: name, laboratory_id, and at least one price (default `price` or any `city_price_<id>`).
        Returns (success, message) — message is user-facing Arabic."""
        try:
            name = form_data.get("name", "").strip()
            duration = form_data.get("duration")
            laboratory_id = form_data.get("laboratory_id") or self.laboratory_id

            if not name:
                return False, "اسم التحليل مطلوب"
            if not laboratory_id:
                return False, "يرجى اختيار المعمل"

            keywords_raw = _parse_tags(
                form_data.getlist("keywords") or form_data.getlist("generated_keywords"),
                form_data.get("keywords")
            )
            alias_raw = _parse_tags(
                form_data.getlist("alias_name") or form_data.getlist("generated_aliases"),
                form_data.get("alias_name")
            )

            service, message = self.create_lab_service(
                laboratory_id=int(laboratory_id),
                name=name,
                price=form_data.get("price"),
                city_prices=_collect_city_prices(form_data),
                duration=int(duration) if duration else None,
                patient_instructions=form_data.get("patient_instructions") or form_data.get("instructions"),
                description=form_data.get("description"),
                keywords=keywords_raw,
                alias_name=alias_raw,
                sample_type=form_data.get("sample_type"),
            )

            if not service:
                return False, message

            return True, "تم إضافة التحليل الطبي بنجاح"

        except ValueError:
            return False, "بيانات السعر أو المدة غير صحيحة"
        except Exception as e:
            logger.exception("handle_create_test failed: %s", e)
            return False, "حدث خطأ أثناء إضافة التحليل"

    def handle_update_test(self, form_data, test_id=None):
        """Validate form_data and update a test. Accepts (form_data, test_id) or the legacy
        inverted (test_id, form_data) call shape — auto-detected.
        Reads the default `price` and the per-city `city_price_<id>` fields.
        Returns (success, message).
        """
        if isinstance(form_data, int) or (isinstance(test_id, dict) or hasattr(test_id, 'getlist')):
            actual_test_id, actual_form_data = form_data, test_id
        else:
            actual_test_id = test_id or self.test_id
            actual_form_data = form_data

        try:
            name = actual_form_data.get("name")
            price = actual_form_data.get("price")
            duration = actual_form_data.get("duration")
            laboratory_id = actual_form_data.get("laboratory_id") or self.laboratory_id

            keywords_raw = _parse_tags(
                actual_form_data.getlist("keywords") or actual_form_data.getlist("generated_keywords"),
                actual_form_data.get("keywords")
            )
            alias_raw = _parse_tags(
                actual_form_data.getlist("alias_name") or actual_form_data.getlist("generated_aliases"),
                actual_form_data.get("alias_name")
            )

            updated_test, message = self.update_test(
                test_id=actual_test_id,
                name=name.strip() if name else None,
                price=price if price not in (None, "") else None,
                duration=int(duration) if duration else None,
                instructions=actual_form_data.get("patient_instructions") or actual_form_data.get("instructions"),
                description=actual_form_data.get("description"),
                keywords=keywords_raw,
                alias_name=alias_raw,
                sample_type=actual_form_data.get("sample_type"),
                laboratory_id=int(laboratory_id) if laboratory_id else None,
                city_prices=_collect_city_prices(actual_form_data),
            )

            if not updated_test:
                return False, message

            return True, "تم تحديث بيانات التحليل بنجاح"

        except ValueError:
            return False, "قيم السعر أو المدة غير صحيحة"
        except Exception as e:
            logger.exception("handle_update_test failed for test_id=%r: %s", actual_test_id, e)
            return False, "حدث خطأ أثناء تحديث التحليل"

    def is_ajax_request(self, request):
        """True if the Flask request is JSON, XHR, or Accepts application/json."""
        return (
            request.is_json
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in request.headers.get("Accept", "")
        )

    def process_ai_generation(self, request):
        """Generate test metadata (description/instructions/keywords) from the request's name. Returns (TestGenerationResult or None, error or None)."""
        try:
            if request.is_json:
                data = request.get_json(silent=True) or {}
                name = data.get("name", "").strip()
                description = data.get("description")
                instructions = data.get("patient_instructions") or data.get("instructions")
            else:
                name = request.form.get("name", "").strip()
                description = request.form.get("description")
                instructions = request.form.get("patient_instructions") or request.form.get("instructions")

            if not name:
                return None, "اسم التحليل مطلوب"

            result = generate_test(name=name, description=description, instructions=instructions)
            return result, None
        except Exception as e:
            logger.exception("process_ai_generation failed: %s", e)
            return None, "حدث خطأ أثناء توليد بيانات التحليل بالذكاء الاصطناعي"

    def process_ai_regeneration(self, request):
        """Regenerate alternative test metadata given the previous AI output in the request. Returns (TestGenerationResult or None, error or None)."""
        try:
            if request.is_json:
                data = request.get_json(silent=True) or {}
                name = data.get("name", "").strip()
                prev_raw = data.get("previous_output") or {}
                previous_output = TestGenerationResult(
                    description=prev_raw.get("description", ""),
                    patient_instructions=prev_raw.get("instructions") or prev_raw.get("patient_instructions", ""),
                    keywords=prev_raw.get("keywords") or [],
                    alias_name=prev_raw.get("aliases") or prev_raw.get("alias_name") or [],
                )
            else:
                name = request.form.get("name", "").strip()
                previous_output = TestGenerationResult(
                    description=request.form.get("generated_description", ""),
                    patient_instructions=request.form.get("generated_instructions", ""),
                    keywords=request.form.getlist("generated_keywords"),
                    alias_name=request.form.getlist("generated_aliases"),
                )

            if not name:
                return None, "اسم التحليل مطلوب"

            result = regenerate_test(name=name, previous_output=previous_output)
            return result, None
        except Exception as e:
            logger.exception("process_ai_regeneration failed: %s", e)
            return None, "حدث خطأ أثناء إعادة توليد بيانات التحليل بالذكاء الاصطناعي"

    def format_ai_response(self, result):
        """Map a TestGenerationResult to the dict shape the dashboard form expects (dual keys for instructions/aliases)."""
        return {
            "description": result.description,
            "instructions": result.patient_instructions,
            "patient_instructions": result.patient_instructions,
            "keywords": result.keywords,
            "aliases": result.alias_name,
            "alias_name": result.alias_name,
            "duration": result.duration,
            "sample_type": result.sample_type,
        }

    @staticmethod
    def get_services_by_ids(service_ids: list):
        """Fetch LabService instances by IDs (city prices eager-loaded)."""
        if not service_ids:
            return []
        try:
            int_ids = [int(sid) for sid in service_ids if str(sid).isdigit()]
            if not int_ids:
                return []
            return (
                LabService.query
                .options(selectinload(LabService.city_prices))
                .filter(LabService.id.in_(int_ids))
                .all()
            )
        except Exception as e:
            logger.exception("[TestsService.get_services_by_ids] Error: %s", e)
            return []

    @staticmethod
    def get_total_tests_count(laboratory_id: int = None, city_id: int = None) -> int:
        """Fetch total count of lab services.
        Pass laboratory_id and/or city_id to scope the count (city = tests available in that city);
        omit both for a global count."""
        try:
            query = LabService.query
            if laboratory_id:
                query = query.filter(LabService.laboratory_id == laboratory_id)
            if city_id:
                query = query.filter(_available_in_city(city_id))
            return query.count()
        except Exception:
            return 0