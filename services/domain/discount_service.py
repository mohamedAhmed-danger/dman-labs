import logging
from models.models import City, Client, Discount, DiscountCity, db

logger = logging.getLogger(__name__)

LAB_ID = 1  # Discount.laboratory_id defaults to 1 (single-lab setup)
DISCOUNT_TYPES = ("percentage", "fixed")

NAME_REQUIRED_MSG = "اسم الخصم مطلوب"
DUPLICATE_DISCOUNT_MSG = "يوجد خصم آخر بنفس هذا الاسم"
INVALID_TYPE_MSG = "نوع الخصم غير صحيح"
INVALID_VALUE_MSG = "قيمة الخصم يجب أن تكون رقماً أكبر من صفر"
INVALID_PERCENT_MSG = "نسبة الخصم لا يمكن أن تتجاوز 100%"
NO_CITY_MSG = "اختر مدينة واحدة على الأقل، أو اجعل الخصم متاحاً لكل المدن"
NOT_FOUND_MSG = "الخصم غير موجود"


class DiscountService:
    def __init__(self, discount_id=None, discount=None):
        """Wrap a single discount, given its ID or a pre-loaded instance."""
        self.discount_id = discount_id
        self._discount = discount
        if discount:
            self.discount_id = discount.id

    @property
    def discount(self):
        """The discount, lazily loaded by ID on first access."""
        if self._discount is None and self.discount_id is not None:
            self._discount = db.session.get(Discount, self.discount_id)
        return self._discount

    # ---------- Helpers ----------
    @staticmethod
    def _clean_text(value):
        return (value or "").strip()

    @staticmethod
    def _parse_value(raw, discount_type):
        """Returns (value, error)."""
        try:
            val = float(raw)
        except (ValueError, TypeError):
            return None, INVALID_VALUE_MSG
        if val <= 0:
            return None, INVALID_VALUE_MSG
        if discount_type == "percentage" and val > 100:
            return None, INVALID_PERCENT_MSG
        return val, None

    @staticmethod
    def _parse_city_links(scope, statuses):
        """
        scope    : "all" or "specific"
        statuses : {city_id: "active" | "inactive" | ""}
        Returns (links, error):
          links = {}                      -> available in ALL cities (no link rows)
          links = {city_id: is_active}    -> only these cities
        """
        if scope != "specific":
            return {}, None

        valid_ids = {c.id for c in City.query.with_entities(City.id).all()}
        links = {}
        for cid, status in (statuses or {}).items():
            if cid not in valid_ids:
                continue
            if status == "active":
                links[cid] = True
            elif status == "inactive":
                links[cid] = False
        if not links:
            return None, NO_CITY_MSG
        return links, None

    @staticmethod
    def _apply_city_links(discount, links):
        """Sync DiscountCity rows with `links`. Does not commit."""
        existing = {l.city_id: l for l in discount.city_links}
        for cid, row in existing.items():
            if cid not in links:
                db.session.delete(row)
        for cid, active in links.items():
            row = existing.get(cid)
            if row:
                row.is_active = active
            else:
                db.session.add(
                    DiscountCity(discount_id=discount.id, city_id=cid, is_active=active)
                )

    def _name_taken(self, name, exclude_id=None):
        q = Discount.query.filter(
            Discount.laboratory_id == LAB_ID,
            db.func.lower(Discount.name) == name.lower(),
        )
        if exclude_id:
            q = q.filter(Discount.id != exclude_id)
        return q.first() is not None

    # ---------- Create ----------
    def create_discount(self, name, discount_type, value, aliases=None, terms=None,
                        is_active=True, scope="all", city_statuses=None):
        try:
            name = self._clean_text(name)
            if not name:
                return None, NAME_REQUIRED_MSG
            if discount_type not in DISCOUNT_TYPES:
                return None, INVALID_TYPE_MSG

            val, err = self._parse_value(value, discount_type)
            if err:
                return None, err

            links, err = self._parse_city_links(scope, city_statuses)
            if err:
                return None, err

            if self._name_taken(name):
                return None, DUPLICATE_DISCOUNT_MSG

            new_discount = Discount(
                laboratory_id=LAB_ID,
                name=name,
                discount_type=discount_type,
                value=val,
                aliases=self._clean_text(aliases),
                terms=self._clean_text(terms),
                is_active=bool(is_active),
            )
            db.session.add(new_discount)
            db.session.flush()  # get the ID without committing yet

            self._apply_city_links(new_discount, links)
            db.session.commit()  # discount + city links saved together

            self._discount = new_discount
            self.discount_id = new_discount.id
            return new_discount, "تم إنشاء الخصم بنجاح"
        except Exception:
            db.session.rollback()
            logger.exception("[DiscountService.create_discount] failed")
            return None, "حدث خطأ أثناء إنشاء الخصم"

    # ---------- Read ----------
    def get_discount(self, discount_id=None):
        target_id = discount_id or self.discount_id
        if not target_id:
            return None, NOT_FOUND_MSG
        try:
            discount = db.session.get(Discount, target_id)
            if not discount:
                return None, NOT_FOUND_MSG
            self._discount = discount
            self.discount_id = discount.id
            return discount, "تم العثور على الخصم"
        except Exception:
            logger.exception("[DiscountService.get_discount] failed")
            return None, "حدث خطأ أثناء جلب الخصم"

    @staticmethod
    def get_all_discounts():
        try:
            return (
                Discount.query.filter_by(laboratory_id=LAB_ID)
                .order_by(Discount.id.asc())
                .all()
            )
        except Exception:
            logger.exception("[DiscountService.get_all_discounts] failed")
            return []

    # ---------- Update ----------
    def update_discount(self, name=None, discount_type=None, value=None,
                        aliases=None, terms=None, is_active=None,
                        scope=None, city_statuses=None, discount_id=None):
        """Fields left as None are not changed. scope=None leaves the cities alone."""
        try:
            target_id = discount_id or self.discount_id
            discount = db.session.get(Discount, target_id) if target_id else None
            if not discount:
                return None, NOT_FOUND_MSG

            # ---- validate everything first, then write ----
            new_name = None
            if name is not None:
                new_name = self._clean_text(name)
                if not new_name:
                    return None, NAME_REQUIRED_MSG
                if self._name_taken(new_name, exclude_id=discount.id):
                    return None, DUPLICATE_DISCOUNT_MSG

            effective_type = discount_type if discount_type is not None else discount.discount_type
            if effective_type not in DISCOUNT_TYPES:
                return None, INVALID_TYPE_MSG

            new_value = None
            if value is not None:
                new_value, err = self._parse_value(value, effective_type)
                if err:
                    return None, err
            elif discount_type is not None:
                # type changed -> re-check the stored value against the new type
                _, err = self._parse_value(discount.value, effective_type)
                if err:
                    return None, err

            links = None
            if scope is not None:
                links, err = self._parse_city_links(scope, city_statuses)
                if err:
                    return None, err

            # ---- apply ----
            if new_name is not None:
                discount.name = new_name
            discount.discount_type = effective_type
            if new_value is not None:
                discount.value = new_value
            if aliases is not None:
                discount.aliases = self._clean_text(aliases)
            if terms is not None:
                discount.terms = self._clean_text(terms)
            if is_active is not None:
                discount.is_active = bool(is_active)
            if links is not None:
                self._apply_city_links(discount, links)

            db.session.commit()
            self._discount = discount
            self.discount_id = discount.id
            return discount, "تم تحديث الخصم بنجاح"
        except Exception:
            db.session.rollback()
            logger.exception("[DiscountService.update_discount] failed")
            return None, "حدث خطأ أثناء تحديث الخصم"

    def toggle_active(self, discount_id=None):
        """Flip the global switch (all cities at once)."""
        try:
            target_id = discount_id or self.discount_id
            discount = db.session.get(Discount, target_id) if target_id else None
            if not discount:
                return None, NOT_FOUND_MSG
            discount.is_active = not discount.is_active
            db.session.commit()
            return discount, ("تم تفعيل الخصم" if discount.is_active else "تم إيقاف الخصم")
        except Exception:
            db.session.rollback()
            logger.exception("[DiscountService.toggle_active] failed")
            return None, "حدث خطأ أثناء تغيير حالة الخصم"

    # ---------- Delete ----------
    def delete_discount(self, discount_id=None):
        target_id = discount_id or self.discount_id
        if not target_id:
            return None, NOT_FOUND_MSG
        try:
            discount = db.session.get(Discount, target_id)
            if not discount:
                return None, NOT_FOUND_MSG
            db.session.delete(discount)  # cascade deletes the city links too
            db.session.commit()
            self._discount = None
            return discount, "تم حذف الخصم بنجاح"
        except Exception:
            db.session.rollback()
            logger.exception("[DiscountService.delete_discount] failed")
            return None, "حدث خطأ أثناء حذف الخصم"

    # ---------- Agent prompt ----------
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
            logger.exception("[DiscountService.get_client_city_id] failed")
            return None

    @staticmethod
    def get_discounts_formatted_context(city_id=None):
        """Active discounts as text for the LLM prompt.
        city_id given  -> discounts available in that city.
        city_id None   -> only discounts that apply to all cities."""
        try:
            discounts = (
                Discount.query.filter_by(laboratory_id=LAB_ID, is_active=True)
                .order_by(Discount.id.asc())
                .all()
            )
            if city_id:
                available = [d for d in discounts if d.is_available_for_city(city_id)]
            else:
                available = [d for d in discounts if not d.city_links]

            if not available:
                return "لا توجد خصومات متاحة حالياً."

            lines = []
            for d in available:
                if d.discount_type == "percentage":
                    value_txt = f"{d.value:g}%"
                else:
                    value_txt = f"{d.value:g} جنيه"
                aliases = f" | يُعرف أيضاً بـ: {d.aliases}" if d.aliases else ""
                terms = f" | الشروط: {d.terms}" if d.terms else ""
                lines.append(f"• {d.name} - الخصم: {value_txt}{aliases}{terms}")
            return "\n".join(lines)
        except Exception:
            logger.exception("[DiscountService.get_discounts_formatted_context] failed")
            return "تعذر جلب معلومات الخصومات."