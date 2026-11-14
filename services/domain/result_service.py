import logging
from datetime import datetime, timezone, timedelta

from models.models import db, ResultRequest, Status

logger = logging.getLogger(__name__)


class ResultService:
    DEDUP_HOURS = 24

    @staticmethod
    def create_request(user_identity, laboratory_id=None, city_id=None):
        """Create a result request unless the same identity already has a recent PENDING one."""
        since = datetime.now(timezone.utc) - timedelta(hours=ResultService.DEDUP_HOURS)
        existing = ResultRequest.query.filter(
            ResultRequest.user_identity == user_identity,
            ResultRequest.status == Status.PENDING,
            ResultRequest.created_at >= since,
        ).first()
        if existing:
            return existing, False

        req = ResultRequest(
            user_identity=user_identity,
            laboratory_id=laboratory_id,
            city_id=city_id,
        )
        try:
            db.session.add(req)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("[ResultService.create_request] Error")
            raise
        return req, True

    @staticmethod
    def list_requests(status=None, search=None, page=1, per_page=20):
        q = ResultRequest.query.order_by(ResultRequest.created_at.desc())
        if status:
            try:
                q = q.filter(ResultRequest.status == Status(status))
            except ValueError:
                pass
        if search:
            q = q.filter(ResultRequest.user_identity.ilike(f"%{search}%"))
        return q.paginate(page=page, per_page=per_page, error_out=False)

    @staticmethod
    def update_status(request_id, status_value):
        req = db.session.get(ResultRequest, request_id)
        if not req or not status_value:
            return None
        try:
            req.status = Status(status_value)
            db.session.commit()
        except ValueError:
            return None
        except Exception:
            db.session.rollback()
            logger.exception("[ResultService.update_status] Error")
            return None
        return req

    @staticmethod
    def delete(request_id):
        req = db.session.get(ResultRequest, request_id)
        if not req:
            return
        try:
            db.session.delete(req)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("[ResultService.delete] Error")

    @staticmethod
    def count_pending():
        return ResultRequest.query.filter_by(status=Status.PENDING).count()