import logging
from sqlalchemy.exc import IntegrityError

from models.models import db, City, Branch, LabServiceCityPrice, Client

logger = logging.getLogger(__name__)


class CityService:
    def __init__(self, city_id=None, city=None, laboratory_id=None):
        self.city_id = city_id
        self._city = city
        self.laboratory_id = laboratory_id
        if city:
            self.city_id = city.id
            if hasattr(city, 'laboratory_id') and city.laboratory_id:
                self.laboratory_id = city.laboratory_id

    @property
    def city(self):
        if self._city is None and self.city_id is not None:
            self._city = db.session.get(City, self.city_id)
            if self._city and hasattr(self._city, 'laboratory_id') and self.laboratory_id is None:
                self.laboratory_id = self._city.laboratory_id
        return self._city

    def create_city(self, laboratory_id: int = None, name: str = None, sort_order: int = 0, aliases=None):
        lab_id = laboratory_id or self.laboratory_id
        try:
            name = (name or "").strip()
            if not lab_id:
                return None, "معرّف المعمل مطلوب"
            if not name:
                return None, "اسم المنطقة مطلوب"

            existing = City.query.filter_by(laboratory_id=lab_id, name=name).first()
            if existing:
                return None, "المنطقة موجودة بالفعل لهذا المعمل"

            new_city = City(
                laboratory_id=lab_id,
                name=name,
                sort_order=int(sort_order or 0),
                aliases=self._normalize_aliases(aliases),
            )
            db.session.add(new_city)
            db.session.commit()
            self._city = new_city
            self.city_id = new_city.id
            self.laboratory_id = new_city.laboratory_id

            return new_city, "تم إضافة المنطقة بنجاح"
        except IntegrityError:
            db.session.rollback()
            return None, "المنطقة موجودة بالفعل لهذا المعمل"
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to create city: %s", e)
            return None, "حدث خطأ أثناء إنشاء المنطقة"

    def get_city(self, city_id: int = None, laboratory_id: int = None):
        target_id = city_id or self.city_id
        lab_id = laboratory_id or self.laboratory_id

        if not target_id and self._city:
            target_id = self._city.id

        if not target_id:
            return None, "المنطقة غير موجودة"

        try:
            city = db.session.get(City, target_id)
            if not city:
                return None, "المنطقة غير موجودة"

            if lab_id and city.laboratory_id != lab_id:
                return None, "ليس لديك صلاحية الوصول إلى هذه المنطقة"

            return city, "تم جلب بيانات المنطقة بنجاح"
        except Exception as e:
            logger.exception("Failed to fetch city %s: %s", target_id, e)
            return None, "حدث خطأ أثناء جلب بيانات المنطقة"

    @staticmethod
    def get_cities_by_laboratory(laboratory_id: int):
        if not laboratory_id:
            return []
        try:
            return City.query.filter_by(laboratory_id=laboratory_id)\
                .order_by(City.sort_order.asc(), City.id.asc()).all()
        except Exception as e:
            logger.exception("Failed to fetch cities for laboratory %s: %s", laboratory_id, e)
            return []

    def update_city(self, city_id: int = None, name: str = None, sort_order: int = None,
                    laboratory_id: int = None, aliases=None):
        target_id = city_id or self.city_id
        lab_id = laboratory_id or self.laboratory_id

        try:
            city, msg = self.get_city(city_id=target_id, laboratory_id=lab_id)
            if not city:
                return None, msg

            if name is not None:
                clean_name = name.strip()
                if not clean_name:
                    return None, "اسم المنطقة مطلوب"
                if clean_name != city.name:
                    duplicate = City.query.filter(
                        City.laboratory_id == city.laboratory_id,
                        City.name == clean_name,
                        City.id != city.id
                    ).first()
                    if duplicate:
                        return None, "اسم المنطقة مستخدم بالفعل"
                    city.name = clean_name

            if sort_order is not None:
                city.sort_order = int(sort_order)
            if aliases is not None:
                city.aliases = self._normalize_aliases(aliases)

            db.session.commit()
            self._city = city
            return city, "تم تحديث بيانات المنطقة بنجاح"
        except IntegrityError:
            db.session.rollback()
            return None, "اسم المنطقة مستخدم بالفعل"
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to update city %s: %s", target_id, e)
            return None, "حدث خطأ أثناء تحديث المنطقة"

    @staticmethod
    def _normalize_aliases(aliases):
        if isinstance(aliases, str):
            aliases = aliases.split(",")
        if not aliases:
            return []
        return list(dict.fromkeys(
            item.strip() for item in aliases
            if isinstance(item, str) and item.strip()
        ))

    @staticmethod
    def reorder_cities(laboratory_id: int, city_orders: list[tuple[int, int]]):
        if not laboratory_id or not city_orders:
            return False, "بيانات الترتيب غير صحيحة"

        try:
            normalized_orders = []
            seen_city_ids = set()
            for entry in city_orders:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    return False, "بيانات الترتيب غير صحيحة"
                city_id, order = (int(value) for value in entry)
                if city_id <= 0 or order < 0 or city_id in seen_city_ids:
                    return False, "بيانات الترتيب غير صحيحة"
                seen_city_ids.add(city_id)
                normalized_orders.append((city_id, order))

            cities = City.query.filter(
                City.id.in_(seen_city_ids),
                City.laboratory_id == laboratory_id,
            ).all()
            if len(cities) != len(normalized_orders):
                return False, "منطقة واحدة أو أكثر غير موجودة لهذا المعمل"

            city_by_id = {city.id: city for city in cities}
            for city_id, order in normalized_orders:
                city_by_id[city_id].sort_order = order
            db.session.commit()
            return True, "تم إعادة ترتيب المناطق بنجاح"
        except (TypeError, ValueError):
            db.session.rollback()
            return False, "بيانات الترتيب غير صحيحة"
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to reorder cities for lab %s: %s", laboratory_id, e)
            return False, "حدث خطأ أثناء إعادة ترتيب المناطق"

    def delete_city(self, city_id: int = None, laboratory_id: int = None):
        target_id = city_id or self.city_id
        lab_id = laboratory_id or self.laboratory_id

        try:
            city, msg = self.get_city(city_id=target_id, laboratory_id=lab_id)
            if not city:
                return False, msg

            has_tests = LabServiceCityPrice.query.filter_by(city_id=city.id).first() is not None
            has_branches = Branch.query.filter_by(city_id=city.id).first() is not None
            has_clients = Client.query.filter_by(city_id=city.id).first() is not None

            if has_tests or has_branches or has_clients:
                return False, "لا يمكن حذف المنطقة لأنها تحتوي على تحاليل أو فروع أو عملاء مرتبطة بها"

            db.session.delete(city)
            db.session.commit()
            self._city = None

            return True, "تم حذف المنطقة بنجاح"
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to delete city %s: %s", target_id, e)
            return False, "حدث خطأ أثناء حذف المنطقة"