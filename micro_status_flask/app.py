import os
import re
import shlex
import sqlite3
import subprocess
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired

import settings
from auth import setup_auth, user_info
from forms import DatasetForm
from models import CLNumber, db, Dataset, PI

app = Flask(__name__)

app, login_manager = setup_auth(app)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////CBI_FastStore/Iana/RSCM_MesoSPIM_datasets.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'your-secret-key'

db.init_app(app)


def move_job_base_name(dataset):
    pi_name = dataset.pi_obj.name if dataset.pi_obj else "unknown_pi"
    cl_name = dataset.cl_obj.name if dataset.cl_obj else "unknown_cl"
    raw_name = f"{str(dataset.id).zfill(5)}_{pi_name}_{cl_name}_{dataset.name}"
    return re.sub(r'[^A-Za-z0-9._-]+', '_', raw_name)


def ensure_move_job_dirs():
    move_dirs = {
        'scripts': os.path.join(settings.MOVE_JOBS_DIR, 'scripts'),
        'logs': os.path.join(settings.MOVE_JOBS_DIR, 'logs'),
        'complete': os.path.join(settings.MOVE_JOBS_DIR, 'complete'),
        'error': os.path.join(settings.MOVE_JOBS_DIR, 'error'),
    }
    for path in move_dirs.values():
        os.makedirs(path, exist_ok=True)
    return move_dirs

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
    # Get filters from query parameters
    date_filter = request.args.get('time', 'week')
    pi_id_filter = request.args.get('name', '')

    # Fetch all PIs for filter dropdown
    pi_options = [(pi.id, pi.name) for pi in PI.query.order_by(PI.name).all()]

    # Build base query
    query = Dataset.query

    # Date filtering
    now = datetime.now()
    if date_filter == 'week':
        since = now - timedelta(weeks=1)
    elif date_filter == 'month':
        since = now - timedelta(days=30)
    elif date_filter == 'year':
        since = now - timedelta(days=365)
    else:
        since = None

    if since:
        query = query.filter(Dataset.created != None)  # Skip NULLs
        query = query.filter(Dataset.created >= since.strftime('%Y-%m-%d'))

    # PI-based filtering
    if current_user.get_id() == "CBI_Admin":
        if pi_id_filter:
            query = query.filter(Dataset.pi == int(pi_id_filter))
    else:
        user_pi = PI.query.filter_by(name=current_user.get_id()).first()
        if user_pi:
            query = query.filter(Dataset.pi == user_pi.id)
            pi_id_filter = user_pi.id  # Persist this to highlight in UI
        else:
            query = query.filter(False)  # Return empty result set

    datasets = query.all()

    return render_template(
        'index.html',
        datasets=datasets,
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
        return redirect(url_for('index'))  # or wherever your list route is

    return render_template('edit_dataset.html', form=form, dataset=dataset)


@app.route('/datasets/<int:dataset_id>/restart', methods=['POST'])
@login_required
def restart_processing(dataset_id):
    dataset = Dataset.query.get_or_404(dataset_id)
    if dataset.modality == "mesospim":
        # cmd = [     # old version for btf only
        #     '/h20/home/lab/miniconda3/envs/mesospim_utils/bin/python',
        #     '/h20/home/lab/src/mesospim_utils/mesospim_utils/automated.py',
        #     'automated-method-slurm',
        #     dataset.path_on_fast_store if ' ' not in dataset.path_on_fast_store else f'"{dataset.path_on_fast_store}"'
        # ]
        cmd = [    # new version for zarr
            '/h20/home/lab/miniconda3/envs/mesospim_dev/bin/python',
            '/h20/home/lab/src/mesospim_utils_v0.1/mesospim_utils/automated.py',
            'automated-method-slurm',
            dataset.path_on_fast_store if ' ' not in dataset.path_on_fast_store else f'"{dataset.path_on_fast_store}"',
            '--final-file-type',
            'ims'
        ]
        try:
            subprocess.run(cmd)
            flash(f"Processing restarted for dataset {dataset.id}.", "success")
        except subprocess.CalledProcessError as e:
            flash(f"Failed to restart processing: {e}", "danger")
    elif dataset.modality == "rscm":
        rscm_txt_file_name = f"{str(dataset.id).zfill(5)}_{dataset.pi_obj.name}_{dataset.cl_obj.name}_{dataset.name}.txt"
        txt_file_path = os.path.join(settings.RSCM_FOLDER_STITCHING, 'queueStitch', rscm_txt_file_name)
        # txt_file_path = os.path.join(settings.RSCM_FOLDER_STITCHING, 'tempQueue', rscm_txt_file_name)
        contents = f'rootDir="{dataset.path_on_fast_store}"\nkeepComposites=True\nmoveToHive=False'
        with open(txt_file_path, "w") as f:
            f.write(contents)

    return redirect(url_for('edit_dataset', dataset_id=dataset.id))


@app.route('/datasets/<int:dataset_id>/move', methods=['POST'])
@login_required
def move_dataset_to_h20(dataset_id):
    dataset = Dataset.query.get_or_404(dataset_id)
    src_path = dataset.path_on_fast_store or ''
    dst_path = src_path.replace(settings.FASTSTORE_ACQUISITION_FOLDER, settings.HIVE_ACQUISITION_FOLDER, 1)

    if src_path.startswith(settings.HIVE_ACQUISITION_FOLDER):
        flash(f"Dataset {dataset.id} is already at /h20. No move was submitted.", "warning")
        return redirect(url_for('edit_dataset', dataset_id=dataset.id))

    if not src_path.startswith(settings.FASTSTORE_ACQUISITION_FOLDER):
        flash(f"Dataset {dataset.id} is not under {settings.FASTSTORE_ACQUISITION_FOLDER}.", "danger")
        return redirect(url_for('edit_dataset', dataset_id=dataset.id))

    if dataset.moving:
        flash(f"Move already in progress for dataset {dataset.id}.", "warning")
        return redirect(url_for('edit_dataset', dataset_id=dataset.id))

    if dataset.moved:
        flash(f"Dataset {dataset.id} has already been marked as moved.", "warning")
        return redirect(url_for('edit_dataset', dataset_id=dataset.id))

    if not os.path.isdir(src_path):
        if os.path.isdir(dst_path):
            flash(
                f"Dataset {dataset.id} was not found on /CBI_FastStore and already exists at /h20. No move was submitted.",
                "warning",
            )
        else:
            flash(f"Source dataset folder does not exist: {src_path}", "danger")
        return redirect(url_for('edit_dataset', dataset_id=dataset.id))

    move_dirs = ensure_move_job_dirs()
    move_base_name = move_job_base_name(dataset)
    move_log_path = os.path.join(move_dirs['logs'], f"{move_base_name}.log")
    move_script_path = os.path.join(move_dirs['scripts'], f"{move_base_name}.sbatch.sh")
    move_complete_marker = os.path.join(move_dirs['complete'], f"{move_base_name}.done")
    move_error_marker = os.path.join(move_dirs['error'], f"{move_base_name}.error")
    trash_path = os.path.join(
        settings.FASTSTORE_TRASH_LOCATION,
        os.path.relpath(src_path, settings.FASTSTORE_ACQUISITION_FOLDER),
    )

    for marker in [move_complete_marker, move_error_marker]:
        if os.path.exists(marker):
            os.remove(marker)

    script_contents = f"""#!/bin/bash
#SBATCH --job-name=move_{str(dataset.id).zfill(5)}
#SBATCH --partition=compute
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --output={move_log_path}
#SBATCH --error={move_log_path}

set -euo pipefail

SRC={shlex.quote(src_path)}
BASE_DST={shlex.quote(dst_path)}
TRASH={shlex.quote(trash_path)}
COMPLETE_MARKER={shlex.quote(move_complete_marker)}
ERROR_MARKER={shlex.quote(move_error_marker)}

cleanup_on_error() {{
    status=$?
    mkdir -p "$(dirname "$ERROR_MARKER")"
    printf 'move failed for %s -> %s (exit %s)\n' "$SRC" "${{DST:-$BASE_DST}}" "$status" > "$ERROR_MARKER"
    exit $status
}}
trap cleanup_on_error ERR

DST="$BASE_DST"
if [ -e "$DST" ]; then
    suffix=2
    while [ -e "${{BASE_DST}}_${{suffix}}" ]; do
        suffix=$((suffix + 1))
    done
    DST="${{BASE_DST}}_${{suffix}}"
fi

mkdir -p "$(dirname "$DST")" "$(dirname "$TRASH")" "$(dirname "$COMPLETE_MARKER")"

rclone copy "$SRC" "$DST" --progress --transfers=4 --checkers=8 --size-only --fast-list
rclone check "$SRC" "$DST" --size-only
mv "$SRC" "$TRASH"
printf '%s\n' "$DST" > "$COMPLETE_MARKER"
rm -f "$ERROR_MARKER"
"""

    with open(move_script_path, 'w') as handle:
        handle.write(script_contents)

    os.chmod(move_script_path, 0o750)
    result = subprocess.run(['sbatch', move_script_path], capture_output=True, text=True)
    if result.returncode != 0:
        flash(f"Failed to submit move job for dataset {dataset.id}: {result.stderr.strip()}", "danger")
        return redirect(url_for('edit_dataset', dataset_id=dataset.id))

    dataset.moving = 1
    db.session.commit()
    flash(f"Move to /h20 submitted for dataset {dataset.id}.", "success")
    return redirect(url_for('edit_dataset', dataset_id=dataset.id))


@app.route('/datasets/new', methods=['GET', 'POST'])
@login_required
def create_dataset():
    form = DatasetForm()

    form.pi.choices = [(pi.id, pi.name) for pi in PI.query.all()]
    form.cl_number.choices = [(cl.id, cl.name) for cl in CLNumber.query.all()]

    if form.validate_on_submit():
        new_dataset = Dataset()
        form.populate_obj(new_dataset)
        new_dataset.created = datetime.now().strftime(settings.DATETIME_FORMAT)
        db.session.add(new_dataset)
        db.session.commit()
        flash("Dataset created successfully!", "success")
        return redirect(url_for('index'))  # or 'index' depending on your list view

    return render_template('create_dataset.html', form=form)



if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=1414)
