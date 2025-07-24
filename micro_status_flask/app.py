from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
from datetime import datetime, timedelta

from flask_login import login_required, current_user

from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired
import os
import subprocess

import settings
from auth import setup_auth, user_info
from forms import DatasetForm
from models import db, Dataset, PI

app = Flask(__name__)

app, login_manager = setup_auth(app)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////CBI_FastStore/Iana/RSCM_MesoSPIM_datasets.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'your-secret-key'

db.init_app(app)

# def get_data():
#     conn = sqlite3.connect(settings.DB_LOCATION)
#     cursor = conn.cursor()
#     cursor.execute("SELECT id, path_on_fast_store, imaging_status, processing_status, moved, moving, paused FROM dataset")
#     columns = [description[0] for description in cursor.description]
#     rows = cursor.fetchall()
#     conn.close()
#     return columns, rows


# Connect to database and fetch data
def get_data(date_filter, name_filter):
    conn = sqlite3.connect(settings.DB_LOCATION)
    cursor = conn.cursor()

    base_query = "SELECT id, path_on_fast_store, imaging_status, processing_status, moved, moving, paused FROM dataset"
    conditions = []
    params = []

    if date_filter == 'week':
        since = datetime.now() - timedelta(weeks=1)
    elif date_filter == 'month':
        since = datetime.now() - timedelta(days=30)
    elif date_filter == 'year':
        since = datetime.now() - timedelta(days=365)
    else:
        since = None

    if since:
        since_str = since.strftime("%Y-%m-%d_%H-%M-%S")
        conditions.append(f'created > "{since_str}"')

    if name_filter:
        # conditions.append(f'pi = {name_filter}')
        conditions.append(f'pi = {name_filter}')

    if conditions:
        base_query += ' WHERE ' + ' AND '.join(conditions)

    print("base_query", base_query)
    cursor.execute(base_query)
    rows = cursor.fetchall()
    columns = [description[0] for description in cursor.description]
    conn.close()
    return columns, rows


@app.route('/')
@login_required
def index():
    date_filter = request.args.get('time', 'week')
    pi_id_filter = request.args.get('name', '')  # still using 'name' as the param, but it's actually the id

    # Fetch PI names
    conn = sqlite3.connect(settings.DB_LOCATION)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM pi")
    pi_options = cursor.fetchall()
    conn.close()

    if current_user.get_id() == "CBI_Admin":
        columns, rows = get_data(date_filter, pi_id_filter)
    else:
        pi_id = [pi_id for pi_id, pi_name in pi_options if pi_name == current_user.get_id()]
        if len(pi_id):
            pi_id_filter = pi_id[0]
            columns, rows = get_data(date_filter, pi_id_filter)
        else:
            columns, rows = [], []

    return render_template(
        'index.html',
        columns=columns,
        rows=rows,
        selected_filter=date_filter,
        selected_name=pi_id_filter,
        pi_options=pi_options,
        current_user=current_user
    )

@app.route('/datasets')
def list_datasets():
    ds = Dataset.query.all()
    return render_template('datasets.html', datasets=ds)


@app.route('/datasets/<int:dataset_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_dataset(dataset_id):
    dataset = Dataset.query.get_or_404(dataset_id)
    form = DatasetForm(obj=dataset)

    # Populate PI choices
    form.pi.choices = [(pi.id, pi.name) for pi in PI.query.order_by(PI.name).all()]

    if form.validate_on_submit():
        form.populate_obj(dataset)
        db.session.commit()
        flash("Dataset updated successfully.", "success")
        return redirect(url_for('list_datasets'))  # or wherever your list route is

    return render_template('edit_dataset.html', form=form, dataset=dataset)


@app.route('/datasets/<int:dataset_id>/restart', methods=['POST'])
@login_required
def restart_processing(dataset_id):
    dataset = Dataset.query.get_or_404(dataset_id)
    if dataset.modality == "mesospim":
        cmd = [
            '/h20/home/lab/miniconda3/envs/mesospim_utils/bin/python',
            '/h20/home/lab/src/mesospim_utils/mesospim_utils/automated.py',
            'automated-method-slurm',
            dataset.path_on_fast_store if ' ' not in dataset.path_on_fast_store else f'"{dataset.path_on_fast_store}"'
        ]
        try:
            subprocess.run(cmd)
            flash(f"Processing restarted for dataset {dataset.id}.", "success")
        except subprocess.CalledProcessError as e:
            flash(f"Failed to restart processing: {e}", "danger")

    return redirect(url_for('edit_dataset', dataset_id=dataset.id))


if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=1414)
