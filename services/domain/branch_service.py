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

    @staticmethod
    def get_city_branches(laboratory_id: int, city_id: int) -> list[Branch]:
        """Fetch branches from the requested city, validated against its laboratory."""
        if not laboratory_id or not city_id:
            raise ValueError("laboratory_id and city_id are required")
        city = City.query.filter_by(
            id=city_id,
            laboratory_id=laboratory_id,
        ).first()
        if not city:
            raise ValueError("City does not belong to the requested laboratory")
        try:
            return (
                Branch.query
                .join(City, Branch.city_id == City.id)
                .filter(
                    Branch.city_id == city_id,
                    City.laboratory_id == laboratory_id,
                )
                .order_by(Branch.id.asc())
                .all()
            )
        except Exception:
            logger.exception(
                "Failed to fetch branches | laboratory_id=%s city_id=%s",
                laboratory_id,
                city_id,
            )
            raise

    @classmethod
    def get_city_phone_numbers(cls, laboratory_id: int, city_id: int) -> list[str]:
        """Return unique contact numbers configured for branches in the city."""
        if not laboratory_id or not city_id:
            return []
        branches = cls.get_city_branches(laboratory_id, city_id)
        return list(dict.fromkeys(
            branch.phone_number.strip()
            for branch in branches
            if branch.phone_number and branch.phone_number.strip()
        ))

    @classmethod
    def format_city_branches_context(cls, laboratory_id: int, city_id: int) -> str:
        city = City.query.filter_by(
            id=city_id,
            laboratory_id=laboratory_id,
        ).first()
        if not city:
            raise ValueError("City does not belong to the requested laboratory")
        branches = cls.get_city_branches(laboratory_id, city_id)
        lines = [f"Branches in the client's region ({city.name}):"]
        if not branches:
            lines.append("No branches or branch details are available for this region.")
        for branch in branches:
            lines.append(
                f"- {branch.name}; address: {branch.address or 'Not available'}; "
                f"phone: {branch.phone_number or 'Not available'}; "
                f"working hours: {branch.working_hours or 'Not available'}"
            )
        return "\n".join(lines)

    def update_branch(self, branch_id: int = None, city_id: int = None, name: str = None, address: str = None,
                      phone_number: str = None, working_hours: str = None, laboratory_id: int = None):
        target_id = branch_id or self.branch_id
        lab_id = laboratory_id or self.laboratory_id

        try:
            branch, msg = self.get_branch(branch_id=target_id, laboratory_id=lab_id)
            if not branch:
                return None, msg

            if city_id is not None:
                try:
                    city_id = int(city_id)
                except (TypeError, ValueError):
                    return None, "يرجى اختيار المنطقة"
                if not city_id:
                    return None, "يرجى اختيار المنطقة"

                city = db.session.get(City, city_id)
                if not city:
                    return None, "المنطقة المختارة غير موجودة"
                if lab_id and city.laboratory_id != lab_id:
                    return None, "المنطقة لا تتبع المعمل الخاص بك"

                branch.city_id = city_id

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
