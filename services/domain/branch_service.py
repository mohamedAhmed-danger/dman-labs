import logging

from models.models import db, Branch, City

logger = logging.getLogger(__name__)


class BranchService:
    def __init__(self, branch_id=None, branch=None, laboratory_id=None):
        self.branch_id = branch_id
        self._branch = branch
        self.laboratory_id = laboratory_id

    @property
    def branch(self):
        if self._branch is None and self.branch_id is not None:
            self._branch = db.session.get(Branch, self.branch_id)
        return self._branch

    def create_branch(self, city_id: int, name: str, address: str = None, phone_number: str = None,
                      working_hours: str = None, laboratory_id: int = None):
        lab_id = laboratory_id or self.laboratory_id
        try:
            name = (name or "").strip()
            if not city_id:
                return None, "يرجى اختيار المنطقة"
            if not name:
                return None, "اسم الفرع مطلوب"

            city = db.session.get(City, city_id)
            if not city:
                return None, "المنطقة المختارة غير موجودة"

            if lab_id and city.laboratory_id != lab_id:
                return None, "المنطقة لا تتبع المعمل الخاص بك"

            new_branch = Branch(
                city_id=city_id,
                name=name,
                address=(address or "").strip() if address else None,
                phone_number=(phone_number or "").strip() if phone_number else None,
                working_hours=(working_hours or "").strip() if working_hours else None,
            )
            db.session.add(new_branch)
            db.session.commit()
            self._branch = new_branch
            self.branch_id = new_branch.id
            return new_branch, "تم إضافة الفرع بنجاح"
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to create branch: %s", e)
            return None, "حدث خطأ أثناء إضافة الفرع"

    def get_branch(self, branch_id: int = None, laboratory_id: int = None):
        target_id = branch_id or self.branch_id
        lab_id = laboratory_id or self.laboratory_id

        if not target_id:
            return None, "الفرع غير موجود"

        try:
            branch = db.session.get(Branch, target_id)
            if not branch:
                return None, "الفرع غير موجود"

            if lab_id and branch.city and branch.city.laboratory_id != lab_id:
                return None, "ليس لديك صلاحية الوصول إلى هذا الفرع"

            return branch, "تم جلب بيانات الفرع بنجاح"
        except Exception as e:
            logger.exception("Failed to fetch branch %s: %s", target_id, e)
            return None, "حدث خطأ أثناء جلب بيانات الفرع"

    @staticmethod
    def get_branches_by_city(city_id: int, laboratory_id: int = None):
        if not city_id:
            return []
        try:
            city = db.session.get(City, city_id)
            if not city:
                return []
            if laboratory_id and city.laboratory_id != laboratory_id:
                return []
            return Branch.query.filter_by(city_id=city_id).order_by(Branch.id.asc()).all()
        except Exception as e:
            logger.exception("Failed to fetch branches for city %s: %s", city_id, e)
            return []

    def update_branch(self, branch_id: int = None, name: str = None, address: str = None,
                      phone_number: str = None, working_hours: str = None, laboratory_id: int = None):
        target_id = branch_id or self.branch_id
        lab_id = laboratory_id or self.laboratory_id

        try:
            branch, msg = self.get_branch(branch_id=target_id, laboratory_id=lab_id)
            if not branch:
                return None, msg

            if name is not None:
                clean_name = name.strip()
                if not clean_name:
                    return None, "اسم الفرع مطلوب"
                branch.name = clean_name

            if address is not None:
                branch.address = address.strip() if address else None

            if phone_number is not None:
                branch.phone_number = phone_number.strip() if phone_number else None

            if working_hours is not None:
                branch.working_hours = working_hours.strip() if working_hours else None

            db.session.commit()
            self._branch = branch
            return branch, "تم تحديث بيانات الفرع بنجاح"
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to update branch %s: %s", target_id, e)
            return None, "حدث خطأ أثناء تحديث بيانات الفرع"

    def delete_branch(self, branch_id: int = None, laboratory_id: int = None):
        target_id = branch_id or self.branch_id
        lab_id = laboratory_id or self.laboratory_id

        try:
            branch, msg = self.get_branch(branch_id=target_id, laboratory_id=lab_id)
            if not branch:
                return False, msg

            db.session.delete(branch)
            db.session.commit()
            self._branch = None
            return True, "تم حذف الفرع بنجاح"
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to delete branch %s: %s", target_id, e)
            return False, "حدث خطأ أثناء حذف الفرع"
