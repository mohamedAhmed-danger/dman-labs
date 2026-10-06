from flask import Blueprint, render_template, request, flash, redirect, url_for
from flask_login import login_required
from services.domain.laboratory_service import LaboratoryService
from services.domain.city_service import CityService
from services.domain.branch_service import BranchService
from models.models import LabService

laboratory_bp = Blueprint('laboratory', __name__, url_prefix='/laboratories')


@laboratory_bp.route('/')
@login_required
def list_laboratories():
    lab_service = LaboratoryService()
    laboratories, _ = lab_service.get_all_laboratories()

    lab_id = request.args.get("laboratory_id", 1, type=int)
    current_lab = next((l for l in laboratories if l.id == lab_id), laboratories[0] if laboratories else None)

    cities_data = []
    if current_lab:
        cities = CityService.get_cities_by_laboratory(current_lab.id)
        for city in cities:
            branches = BranchService.get_branches_by_city(city.id, laboratory_id=current_lab.id)
            test_count = LabService.query.filter_by(city_id=city.id).count()
            cities_data.append({
                "city": city,
                "branches": branches,
                "test_count": test_count,
            })

    return render_template(
        'laboratory/list.html',
        laboratories=laboratories or [],
        current_lab=current_lab,
        cities_data=cities_data,
    )


@laboratory_bp.route('/create', methods=['GET', 'POST'])
@login_required
def create_laboratory():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        location = request.form.get('location', '').strip()
        description = request.form.get('description', '').strip()

        lab_service = LaboratoryService()
        lab, msg = lab_service.create_laboratory(name, location, description)

        if lab:
            flash(msg, 'success')
            return redirect(url_for('laboratory.list_laboratories'))

        flash(msg, 'error')

    return render_template('laboratory/create.html')


@laboratory_bp.route('/<int:lab_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_laboratory(lab_id):
    lab_service = LaboratoryService(lab_id=lab_id)
    lab, msg = lab_service.get_laboratory()
    if not lab:
        flash(msg, 'error')
        return redirect(url_for('laboratory.list_laboratories'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        location = request.form.get('location', '').strip()
        description = request.form.get('description', '').strip()

        updated_lab, update_msg = lab_service.update_laboratory(name=name, location=location, description=description)
        if updated_lab:
            flash(update_msg, 'success')
            return redirect(url_for('laboratory.list_laboratories'))

        flash(update_msg, 'error')

    return render_template('laboratory/edit.html', laboratory=lab)