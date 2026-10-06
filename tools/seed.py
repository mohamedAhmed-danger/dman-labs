import sys
import os
import logging

# Ensure project root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from models.models import db, Laboratory, City
from services.shared.vector_service import ensure_collection

logger = logging.getLogger(__name__)

DEFAULT_CITIES = [
    {
        "name": "القاهرة",
        "aliases": ["cairo", "القاهره", "القاهرة"],
        "sort_order": 1,
    },
    {
        "name": "الإسماعيلية",
        "aliases": ["ismailia", "الاسماعيلية", "الإسماعيلية"],
        "sort_order": 2,
    },
    {
        "name": "العريش",
        "aliases": ["al arish", "العريش"],
        "sort_order": 3,
    },
    {
        "name": "أرض اللواء",
        "aliases": ["ard el lewa", "ارض اللواء", "أرض اللواء"],
        "sort_order": 4,
    },
]


def seed_catalog():
    app = create_app()
    with app.app_context():
        # Ensure database tables exist
        db.create_all()

        lab = Laboratory.query.order_by(Laboratory.id.asc()).first()
        if not lab:
            lab = Laboratory(
                name="معامل ضمان للتحاليل الطبية",
                location="القاهرة",
                description="معمل متخصص في جميع التحاليل الطبية والكيميائية",
            )
            db.session.add(lab)
            db.session.commit()
            print(f"Created primary laboratory (ID: {lab.id})")
        else:
            print(f"Found existing laboratory (ID: {lab.id}, Name: {lab.name})")

        seeded_cities = []
        for city_data in DEFAULT_CITIES:
            city = City.query.filter_by(
                laboratory_id=lab.id,
                name=city_data["name"]
            ).first()

            if not city:
                city = City(
                    laboratory_id=lab.id,
                    name=city_data["name"],
                    aliases=city_data["aliases"],
                    sort_order=city_data["sort_order"],
                )
                db.session.add(city)
                db.session.commit()
                print(f"Seeded city: {city.name} (ID: {city.id})")
            else:
                city.aliases = city_data["aliases"]
                city.sort_order = city_data["sort_order"]
                db.session.commit()
                print(f"City already exists: {city.name} (ID: {city.id}) - updated sort_order & aliases")

            # Create / verify corresponding empty Qdrant collection
            col_name = ensure_collection(laboratory_id=lab.id, city_id=city.id)
            seeded_cities.append((city.id, city.name, col_name))

        print("\n--- Seeding Summary ---")
        print(f"Laboratory ID: {lab.id}")
        for city_id, city_name, col in seeded_cities:
            print(f"City ID: {city_id} | Name: {city_name} | Qdrant Collection: {col}")

        print("Seeding completed successfully!")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_catalog()
