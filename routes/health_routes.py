import time
import os
import platform
import sys
from datetime import datetime, timezone
from flask import Blueprint, render_template, jsonify
from flask_login import login_required
from sqlalchemy import text

from models.models import db, User, Booking, Inquiry, Complaint, Laboratory, LabService

health_bp = Blueprint('health', __name__, url_prefix='/system-health')

APP_START_TIME = datetime.now(timezone.utc)

def get_system_metrics():
    """Retrieve system CPU, memory, and disk usage metrics safely."""
    metrics = {
        "cpu_usage": 0,
        "memory_percent": 0,
        "memory_used_mb": 0,
        "memory_total_mb": 0,
        "disk_percent": 0,
        "disk_free_gb": 0,
        "disk_total_gb": 0,
        "python_version": sys.version.split()[0],
        "os_info": f"{platform.system()} {platform.release()}",
        "uptime_str": ""
    }

    # Calculate app uptime
    uptime_delta = datetime.now(timezone.utc) - APP_START_TIME
    hours, remainder = divmod(int(uptime_delta.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    metrics["uptime_str"] = f"{hours}h {minutes}m {seconds}s"

    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        metrics["cpu_usage"] = round(cpu, 1)
        metrics["memory_percent"] = round(mem.percent, 1)
        metrics["memory_used_mb"] = round(mem.used / (1024 * 1024), 1)
        metrics["memory_total_mb"] = round(mem.total / (1024 * 1024), 1)
        metrics["disk_percent"] = round(disk.percent, 1)
        metrics["disk_free_gb"] = round(disk.free / (1024 * 1024 * 1024), 1)
        metrics["disk_total_gb"] = round(disk.total / (1024 * 1024 * 1024), 1)
    except Exception:
        # Standard library fallbacks if psutil is not installed
        try:
            import shutil
            total, used, free = shutil.disk_usage("/")
            metrics["disk_percent"] = round((used / total) * 100, 1)
            metrics["disk_free_gb"] = round(free / (1024 * 1024 * 1024), 1)
            metrics["disk_total_gb"] = round(total / (1024 * 1024 * 1024), 1)
        except Exception:
            pass

    return metrics


@health_bp.route('/')
@login_required
def health_dashboard():
    """Render the System Health Dashboard HTML page."""
    return render_template('system_health.html')


@health_bp.route('/api/status')
@login_required
def health_api_status():
    """JSON endpoint returning live system health diagnostic info."""
    start_time = time.time()
    db_status = "offline"
    db_latency_ms = 0
    db_engine_name = "unknown"

    # Database connectivity & latency test
    try:
        db.session.execute(text("SELECT 1"))
        db_latency_ms = round((time.time() - start_time) * 1000, 2)
        db_status = "online"
        db_engine_name = db.engine.name
    except Exception as e:
        db_status = "error"

    # Fetch database row counts
    counts = {}
    try:
        counts = {
            "users": User.query.count(),
            "bookings": Booking.query.count(),
            "inquiries": Inquiry.query.count(),
            "complaints": Complaint.query.count(),
            "laboratories": Laboratory.query.count(),
            "services": LabService.query.count(),
        }
    except Exception:
        counts = {"users": 0, "bookings": 0, "inquiries": 0, "complaints": 0, "laboratories": 0, "services": 0}

    # Uploads directory status check
    uploads_dir = os.path.join(os.getcwd(), "static", "uploads")
    uploads_writable = os.access(uploads_dir, os.W_OK) if os.path.exists(uploads_dir) else False

    system_metrics = get_system_metrics()

    is_overall_healthy = db_status == "online" and system_metrics["memory_percent"] < 95

    return jsonify({
        "overall_status": "healthy" if is_overall_healthy else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": {
            "status": db_status,
            "latency_ms": db_latency_ms,
            "engine": db_engine_name
        },
        "system": system_metrics,
        "counts": counts,
        "storage": {
            "uploads_dir_exists": os.path.exists(uploads_dir),
            "uploads_writable": uploads_writable
        }
    })
