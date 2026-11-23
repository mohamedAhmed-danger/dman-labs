import logging
from sqlalchemy.exc import IntegrityError
from models.models import Bundle, BundleCityPrice, City, Client, db

logger = logging.getLogger(__name__)

DUPLICATE_BUNDLE_MSG = "يوجد باقة أخرى بنفس هذا الاسم"
BUNDLE_REQUIRED_MSG = "اسم الباقة مطلوب"
BUNDLE_LIMIT_REACHED_MSG = "عذراً، الحد الأقصى للباقات هو 25 باقة فقط"
INVALID_PRICE_MSG = "سعر الباقة غير صحيح"
NOT_FOUND_MSG = "الباقة غير موجودة"


class BundleService:
    def __init__(self, bundle_id=None, bundle=None):
        """Wrap a single bundle, given its ID or a pre-loaded instance."""
        self.bundle_id = bundle_id
        self._bundle = bundle
        if bundle:
            self.bundle_id = bundle.id

    @property
    def bundle(self):
        """The bundle, lazily loaded by ID on first access."""
        if self._bundle is None and self.bundle_id is not None:
            self._bundle = db.session.get(Bundle, self.bundle_id)
        return self._bundle

    @staticmethod
    def _clean_text(value):
        """Strip a text field; empty input becomes an empty string."""
        return (value or "").strip()

    @staticmethod
    def _parse_price(raw):
        """Returns (value, error). Empty input -> (None, None)."""
        if raw is None or str(raw).strip() == "":
            return None, None
        try:
            val = float(raw)
        except (ValueError, TypeError):
            return None, "سعر الباقة يجب أن يكون رقماً صحيحاً"
        if val < 0:
            return None, INVALID_PRICE_MSG
        return val, None

    # ---------- Create ----------
    def create_bundle(self, name, price=None, description=None,
                      instructions=None, city_prices=None):
        """city_prices: optional {city_id: "price"}; empty values are skipped."""
        try:
            name = self._clean_text(name)
            if not name:
                return None, BUNDLE_REQUIRED_MSG

            price_val, err = self._parse_price(price)  # default price is optional
            if err:
                return None, err

            # Validate city prices before touching the DB
            parsed_city_prices = {}
            valid_city_ids = {c.id for c in City.query.with_entities(City.id).all()}
            for cid, raw in (city_prices or {}).items():
                if cid not in valid_city_ids:
                    continue
                val, err = self._parse_price(raw)
                if err:
                    return None, err
                if val is not None:
                    parsed_city_prices[cid] = val

            if Bundle.query.count() >= 25:
                return None, BUNDLE_LIMIT_REACHED_MSG

            if Bundle.query.filter_by(name=name).first():
                return None, DUPLICATE_BUNDLE_MSG

            new_bundle = Bundle(
                name=name,
                price=price_val,
                description=self._clean_text(description),
                instructions=self._clean_text(instructions),
            )
            db.session.add(new_bundle)
            db.session.flush()  # get new_bundle.id without committing yet

            for cid, val in parsed_city_prices.items():
                db.session.add(
                    BundleCityPrice(bundle_id=new_bundle.id, city_id=cid, price=val)
                )

            db.session.commit()  # bundle + city prices saved together

            self._bundle = new_bundle
            self.bundle_id = new_bundle.id
            return new_bundle, "تم إنشاء الباقة بنجاح"
        except IntegrityError:
            db.session.rollback()
            return None, DUPLICATE_BUNDLE_MSG
        except Exception:
            db.session.rollback()
            logger.exception("[BundleService.create_bundle] failed")
            return None, "حدث خطأ أثناء إنشاء الباقة"

    # ---------- Read ----------
    def get_bundle(self, bundle_id=None):
        """Get a bundle by ID. Returns (bundle, message)."""
        target_id = bundle_id or self.bundle_id
        if not target_id and self._bundle:
            return self._bundle, "تم العثور على الباقة"
        if not target_id:
            return None, NOT_FOUND_MSG

        if self._bundle and self._bundle.id == target_id:
            return self._bundle, "تم العثور على الباقة"

        try:
            bundle = db.session.get(Bundle, target_id)
            if not bundle:
                return None, NOT_FOUND_MSG

            if target_id == self.bundle_id or self.bundle_id is None:
                self._bundle = bundle
                self.bundle_id = bundle.id
            return bundle, "تم العثور على الباقة"
        except Exception:
            logger.exception("[BundleService.get_bundle] failed")
            return None, "حدث خطأ أثناء جلب الباقة"

    # ---------- Update ----------
    def update_bundle(self, name=None, price=None, description=None,
                      instructions=None, bundle_id=None):
        """Update a bundle. Fields left as None are not changed.
        An empty-string price clears the default price."""
        try:
            target_id = bundle_id or self.bundle_id
            if not target_id:
                return None, NOT_FOUND_MSG

            bundle = db.session.get(Bundle, target_id)
            if not bundle:
                return None, NOT_FOUND_MSG

            if name is not None:
                new_name = self._clean_text(name)
                if not new_name:
                    return None, BUNDLE_REQUIRED_MSG
                duplicate = Bundle.query.filter(
                    Bundle.name == new_name, Bundle.id != bundle.id
                ).first()
                if duplicate:
                    return None, DUPLICATE_BUNDLE_MSG
                bundle.name = new_name

            if price is not None:
                price_val, err = self._parse_price(price)
                if err:
                    return None, err
                bundle.price = price_val  # None when the field is empty

            if description is not None:
                bundle.description = self._clean_text(description)

            if instructions is not None:
                bundle.instructions = self._clean_text(instructions)

            db.session.commit()
            self._bundle = bundle
            self.bundle_id = bundle.id
            return bundle, "تم تحديث الباقة بنجاح"
        except IntegrityError:
            db.session.rollback()
            return None, DUPLICATE_BUNDLE_MSG
        except Exception:
            db.session.rollback()
            logger.exception("[BundleService.update_bundle] failed")
            return None, "حدث خطأ أثناء تحديث الباقة"

    # ---------- City prices ----------
    def set_city_prices(self, prices):
        """
        prices: {city_id: "price"}
        - price given -> add or update
        - empty       -> delete this city's price
        """
        try:
            bundle = self.bundle
            if not bundle:
                return None, NOT_FOUND_MSG

            existing = {cp.city_id: cp for cp in bundle.city_prices}
            valid_city_ids = {c.id for c in City.query.with_entities(City.id).all()}

            for city_id, raw in prices.items():
                if city_id not in valid_city_ids:
                    continue

                val, err = self._parse_price(raw)
                if err:
                    db.session.rollback()
                    return None, err

                row = existing.get(city_id)
                if val is None:
                    if row:
                        db.session.delete(row)
                elif row:
                    row.price = val
                else:
                    db.session.add(
                        BundleCityPrice(bundle_id=bundle.id, city_id=city_id, price=val)
                    )

            db.session.commit()
            return bundle, "تم حفظ أسعار المدن بنجاح"
        except Exception:
            db.session.rollback()
            logger.exception("[BundleService.set_city_prices] failed")
            return None, "حدث خطأ أثناء حفظ أسعار المدن"

    # ---------- Delete ----------
    def delete_bundle(self, bundle_id=None):
        """Delete a bundle. Returns (deleted_bundle, message)."""
        target_id = bundle_id or self.bundle_id
        if not target_id:
            return None, NOT_FOUND_MSG

        try:
            bundle = db.session.get(Bundle, target_id)
            if not bundle:
                return None, NOT_FOUND_MSG

            db.session.delete(bundle)  # cascade deletes the city prices too
            db.session.commit()
            self._bundle = None
            return bundle, "تم حذف الباقة بنجاح"
        except Exception:
            db.session.rollback()
            logger.exception("[BundleService.delete_bundle] failed")
            return None, "حدث خطأ أثناء حذف الباقة"

    @staticmethod
    def get_all_bundles():
        """Return all bundles ordered by ID."""
        try:
            return Bundle.query.order_by(Bundle.id.asc()).all()
        except Exception:
            logger.exception("[BundleService.get_all_bundles] failed")
            return []

    @staticmethod
    def get_client_city_id(platform_id, page_id, sender_id):
        """Return the client's saved city_id, or None if unknown."""
        try:
            client = Client.query.filter_by(
                sender_id=str(sender_id),
                page_id=str(page_id),
                platform_id=platform_id,
            ).first()
            return client.city_id if client else None
        except Exception:
            logger.exception("[BundleService.get_client_city_id] failed")
            return None

    @staticmethod
    def get_bundles_formatted_context(city_id=None):
        """Bundles as text for the LLM prompt.
        If city_id is given, shows that city's price (or the default price)."""
        try:
            bundles = Bundle.query.order_by(Bundle.id.asc()).all()
            if not bundles:
                return "لا توجد باقات فحصية مضافة حالياً."

            lines = []
            for b in bundles:
                if city_id:
                    price = b.get_price_for_city(city_id)
                    if price is None:
                        continue  # bundle not available in the client's city
                    price_txt = f"{price:g} جنيه"
                else:
                    price_txt = (
                        f"{b.price:g} جنيه" if b.price is not None
                        else "السعر يختلف حسب المدينة"
                    )

                desc = f" | التفاصيل: {b.description}" if b.description else ""
                instr = f" | التعليمات: {b.instructions}" if b.instructions else ""
                lines.append(f"• {b.name} - السعر: {price_txt}{desc}{instr}")

            if not lines:
                return "لا توجد باقات متاحة في مدينتك حالياً."
            return "\n".join(lines)
        except Exception:
            logger.exception("[BundleService.get_bundles_formatted_context] failed")
            return "تعذر جلب معلومات الباقات."