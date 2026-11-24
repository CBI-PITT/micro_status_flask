import base64
import getpass
import hashlib
import hmac
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timedelta

from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import login_required, current_user
from itsdangerous import BadSignature, URLSafeTimedSerializer

import settings
from auth import setup_auth
from forms import DatasetForm, EditDatasetForm, RSCMEditDatasetForm
from mosaic import (MosaicError, brightness_data, dataset_directory, list_acquisitions,
                    load_gains, load_group, render_patch, save_gains, save_positions,
                    view_data)
from models import CLNumber, db, Dataset, PI

app = Flask(__name__)

app, login_manager = setup_auth(app)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////CBI_FastStore/Iana/RSCM_MesoSPIM_datasets.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

MOSAIC_TOKEN_SALT = 'mosaic-positions'
MOSAIC_TOKEN_MAX_AGE = 24 * 60 * 60
BRIGHTNESS_TOKEN_SALT = 'mosaic-brightness'
VIRTUAL_ZARR_TOKEN_SALT = 'virtual-zarr-viewer'
VIRTUAL_ZARR_TOKEN_MAX_AGE = 24 * 60 * 60
IMARIS_EXPORT_TOKEN_SALT = 'imaris-export'


def mosaic_token_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt=MOSAIC_TOKEN_SALT)


def brightness_token_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt=BRIGHTNESS_TOKEN_SALT)


def virtual_zarr_token_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt=VIRTUAL_ZARR_TOKEN_SALT)


def imaris_export_token_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt=IMARIS_EXPORT_TOKEN_SALT)


def virtual_zarr_data_token(dataset_id, path):
    """Expiring per-dataset access token for the virtual Zarr service.

    HMAC format shared with mesospim_virtual_zarr.service.verify_token; the
    service must be started with the same secret as this app's secret key.
    """
    expiry = int(time.time()) + VIRTUAL_ZARR_TOKEN_MAX_AGE
    payload = f"{dataset_id}|{expiry}|{path}"
    signature = hmac.new(app.secret_key.encode('utf-8'), payload.encode('utf-8'),
                         hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(payload.encode('utf-8')).decode('ascii')
    return f"{encoded}.{signature}"


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


DATASET_LIST_TITLES = {
    'mesospim': 'MesoSPIM Acquisition Data',
    'rscm': 'RSCM Acquisition Data',
}


def modality_list_endpoint(modality):
    return 'rscm_datasets' if (modality or '').lower() == 'rscm' else 'index'


def render_dataset_list(modality):
    # Get filters from query parameters
    date_filter = request.args.get('time', 'week')
    pi_id_filter = request.args.get('name', '')

    # Fetch all PIs for filter dropdown
    pi_options = [(pi.id, pi.name) for pi in PI.query.order_by(PI.name).all()]

    # Build base query, restricted to this page's modality
    query = Dataset.query.filter(Dataset.modality == modality)

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

    # Most recent first; NULL created rows sort last, id breaks same-second ties
    query = query.order_by(Dataset.created.desc(), Dataset.id.desc())

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

    nav_query = {'time': date_filter}
    if pi_id_filter:
        nav_query['name'] = str(pi_id_filter)

    return render_template(
        'index.html',
        datasets=datasets,
        modality=modality,
        page_title=DATASET_LIST_TITLES[modality],
        selected_filter=date_filter,
        selected_name=pi_id_filter,
        pi_options=pi_options,
        nav_query=nav_query,
        current_user=current_user
    )


@app.route('/')
@login_required
def index():
    return render_dataset_list('mesospim')


@app.route('/rscm')
@login_required
def rscm_datasets():
    return render_dataset_list('rscm')


@app.route('/datasets/<int:dataset_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_dataset(dataset_id):
    dataset = Dataset.query.get_or_404(dataset_id)
    if (dataset.modality or '').lower() == 'rscm':
        form = RSCMEditDatasetForm(obj=dataset)
    else:
        form = EditDatasetForm(obj=dataset)

    # Populate PI choices
    form.pi.choices = [(pi.id, pi.name) for pi in PI.query.order_by(PI.name).all()]
    # Actual CL numbers as labels, database ids as values
    form.cl_number.choices = ([('', '-- None --')] +
                              [(cl.id, cl.name) for cl in CLNumber.query.order_by(CLNumber.name).all()])

    if form.validate_on_submit():
        form.populate_obj(dataset)
        db.session.commit()
        flash("Dataset updated successfully.", "success")
        return redirect(url_for(modality_list_endpoint(dataset.modality)))

    show_mosaic = False
    if (dataset.modality or '').lower() == 'mesospim':
        try:
            show_mosaic = bool(list_acquisitions(dataset_directory(dataset)))
        except (MosaicError, OSError):
            pass
    return render_template('edit_dataset.html', form=form, dataset=dataset,
                           show_mosaic=show_mosaic,
                           imaris_export_token=imaris_export_token_serializer().dumps(
                               [current_user.get_id(), dataset_id]),
                           virtual_zarr_token=virtual_zarr_token_serializer().dumps(
                               [current_user.get_id(), dataset_id]))


def mosaic_dataset(dataset_id):
    dataset = Dataset.query.get_or_404(dataset_id)
    if (dataset.modality or '').lower() != 'mesospim':
        abort(404)
    return dataset


def prepare_virtual_mosaic(dataset):
    """Reuse aligned tiles, or save stage positions for the first acquisition."""
    root = dataset_directory(dataset)
    acquisitions = list_acquisitions(root)
    if not acquisitions:
        raise MosaicError('No MesoSPIM OME-Zarr 0.4 acquisitions were found.')
    acquisition = 0
    for index in range(len(acquisitions)):
        _, manifest = load_group(root, index)
        if manifest is not None:
            acquisition = index
            break
    group, manifest = load_group(root, acquisition)
    if manifest is None:
        save_positions(root, group, group['stage_positions'])
    return root, group


@app.route('/datasets/<int:dataset_id>/mosaic')
@login_required
def mosaic_view(dataset_id):
    dataset = mosaic_dataset(dataset_id)
    try:
        root = dataset_directory(dataset)
        acquisitions = list_acquisitions(root)
        if not acquisitions:
            raise MosaicError('No MesoSPIM OME-Zarr 0.4 acquisitions were found.')
        acquisition = request.args.get('acquisition', default=0, type=int)
        group, manifest = load_group(root, acquisition)
        data = view_data(root, group, manifest)
    except MosaicError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('edit_dataset', dataset_id=dataset_id))
    token = mosaic_token_serializer().dumps(
        [current_user.get_id(), dataset_id, acquisition]
    )
    return render_template('mosaic_view.html', dataset=dataset, data=data,
                           acquisitions=[str(p.relative_to(root)) for p in acquisitions],
                           acquisition=acquisition, csrf_token=token)


@app.route('/datasets/<int:dataset_id>/mosaic/tiles/<int:acquisition>/<int:tile_index>/<int:level_index>/<int:col>/<int:row>.png')
@login_required
def mosaic_patch(dataset_id, acquisition, tile_index, level_index, col, row):
    dataset = mosaic_dataset(dataset_id)
    try:
        root = dataset_directory(dataset)
        group, _ = load_group(root, acquisition)
        z = int(request.args['z'])
        low = float(request.args['lo'])
        high = float(request.args['hi'])
        image = render_patch(group, tile_index, level_index, col, row, z, low, high)
    except (MosaicError, ValueError, KeyError) as exc:
        abort(400, description=str(exc))
    return Response(image, mimetype='image/png',
                    headers={'Cache-Control': 'private, max-age=60'})


@app.route('/datasets/<int:dataset_id>/mosaic/positions/<int:acquisition>', methods=['POST'])
@login_required
def save_mosaic_positions(dataset_id, acquisition):
    dataset = mosaic_dataset(dataset_id)
    try:
        token_data = mosaic_token_serializer().loads(
            request.headers.get('X-CSRF-Token', ''),
            max_age=MOSAIC_TOKEN_MAX_AGE,
        )
    except BadSignature:
        return jsonify(error='Save authorization expired or invalid. Reopen the viewer.'), 403
    if token_data != [current_user.get_id(), dataset_id, acquisition]:
        return jsonify(error='Save authorization does not match this dataset.'), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400, description='Expected JSON with positions_zyx.')
    try:
        root = dataset_directory(dataset)
        group, _ = load_group(root, acquisition)
        path = save_positions(root, group, body.get('positions_zyx'))
    except MosaicError as exc:
        return jsonify(error=str(exc)), 400
    except OSError as exc:
        return jsonify(error=f'Could not save mosaic.json: {exc}'), 500
    return jsonify(message=f'Saved positions to {path}.')


@app.route('/datasets/<int:dataset_id>/brightness')
@login_required
def brightness_view(dataset_id):
    dataset = mosaic_dataset(dataset_id)
    try:
        root = dataset_directory(dataset)
        acquisitions = list_acquisitions(root)
        if not acquisitions:
            raise MosaicError('No MesoSPIM OME-Zarr 0.4 acquisitions were found.')
        acquisition = request.args.get('acquisition', default=0, type=int)
        group, manifest = load_group(root, acquisition)
        data = brightness_data(group, manifest, load_gains(root, group))
    except MosaicError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('edit_dataset', dataset_id=dataset_id))
    token = brightness_token_serializer().dumps(
        [current_user.get_id(), dataset_id, acquisition]
    )
    return render_template('brightness_view.html', dataset=dataset, data=data,
                           acquisitions=[str(p.relative_to(root)) for p in acquisitions],
                           acquisition=acquisition, csrf_token=token)


@app.route('/datasets/<int:dataset_id>/brightness/tiles/<int:acquisition>/'
           '<int:channel_index>/<int:tile_index>/<int:level_index>/<int:col>/<int:row>.png')
@login_required
def brightness_patch(dataset_id, acquisition, channel_index, tile_index, level_index, col, row):
    dataset = mosaic_dataset(dataset_id)
    try:
        root = dataset_directory(dataset)
        group, _ = load_group(root, acquisition)
        z = int(request.args['z'])
        low = float(request.args['lo'])
        high = float(request.args['hi'])
        gain = float(request.args.get('gain', 1.0))
        image = render_patch(group, tile_index, level_index, col, row, z, low, high,
                             channel_index=channel_index, gain=gain)
    except (MosaicError, ValueError, KeyError) as exc:
        abort(400, description=str(exc))
    return Response(image, mimetype='image/png',
                    headers={'Cache-Control': 'private, max-age=60'})


@app.route('/datasets/<int:dataset_id>/brightness/<int:acquisition>', methods=['POST'])
@login_required
def save_brightness(dataset_id, acquisition):
    dataset = mosaic_dataset(dataset_id)
    try:
        token_data = brightness_token_serializer().loads(
            request.headers.get('X-CSRF-Token', ''),
            max_age=MOSAIC_TOKEN_MAX_AGE,
        )
    except BadSignature:
        return jsonify(error='Save authorization expired or invalid. Reopen the viewer.'), 403
    if token_data != [current_user.get_id(), dataset_id, acquisition]:
        return jsonify(error='Save authorization does not match this dataset.'), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400, description='Expected JSON with channels.')
    try:
        root = dataset_directory(dataset)
        group, _ = load_group(root, acquisition)
        path = save_gains(root, group, body.get('channels'))
    except MosaicError as exc:
        return jsonify(error=str(exc)), 400
    except OSError as exc:
        return jsonify(error=f'Could not save gain.json: {exc}'), 500
    return jsonify(message=f'Saved brightness adjustments to {path}.')


@app.route('/datasets/<int:dataset_id>/virtual_stitched', methods=['POST'])
@login_required
def open_virtual_stitched(dataset_id):
    dataset = mosaic_dataset(dataset_id)
    try:
        token_data = virtual_zarr_token_serializer().loads(
            request.form.get('token', ''), max_age=VIRTUAL_ZARR_TOKEN_MAX_AGE)
    except BadSignature:
        abort(403, description='View authorization expired or invalid. Reload the edit page.')
    if token_data != [current_user.get_id(), dataset_id]:
        abort(403, description='View authorization does not match this dataset.')
    try:
        root, _ = prepare_virtual_mosaic(dataset)
    except MosaicError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('edit_dataset', dataset_id=dataset_id))
    except OSError as exc:
        flash(f'Could not prepare the virtual stitched store: {exc}', 'danger')
        return redirect(url_for('edit_dataset', dataset_id=dataset_id))
    token = virtual_zarr_data_token(dataset_id, str(root))
    return redirect(f"{settings.VIRTUAL_ZARR_SERVICE_PATH.rstrip('/')}/"
                    f"datasets/{dataset_id}/viewer?token={token}")


@app.route('/datasets/<int:dataset_id>/export_imaris', methods=['POST'])
@login_required
def export_dataset_to_imaris(dataset_id):
    dataset = mosaic_dataset(dataset_id)
    try:
        token_data = imaris_export_token_serializer().loads(
            request.form.get('token', ''), max_age=VIRTUAL_ZARR_TOKEN_MAX_AGE)
    except BadSignature:
        abort(403, description='Export authorization expired or invalid. Reload the edit page.')
    if token_data != [current_user.get_id(), dataset_id]:
        abort(403, description='Export authorization does not match this dataset.')

    try:
        root, group = prepare_virtual_mosaic(dataset)
        acquisition_name = group['name'] if group['name'] != '.' else root.name
        base_name = re.sub(r'[^A-Za-z0-9._-]+', '_',
                           acquisition_name.removesuffix('.ome.zarr'))
        output = root / 'virtual_ims' / f'{base_name}.ims'
        if output.exists():
            flash(f'Imaris export already exists: {output}', 'warning')
            return redirect(url_for('edit_dataset', dataset_id=dataset_id))

        job_name = f'imaris_{dataset_id:05d}'
        queued = subprocess.run(
            ['squeue', '--noheader', f'--user={getpass.getuser()}',
             f'--name={job_name}', '--format=%i'], capture_output=True, text=True)
        if queued.returncode != 0:
            flash(f'Could not check Imaris export jobs: {queued.stderr.strip()}', 'danger')
            return redirect(url_for('edit_dataset', dataset_id=dataset_id))
        if queued.stdout.strip():
            flash(f'Imaris export already queued or running for dataset {dataset_id}.', 'warning')
            return redirect(url_for('edit_dataset', dataset_id=dataset_id))

        scripts_dir = os.path.join(settings.IMARIS_EXPORT_JOBS_DIR, 'scripts')
        logs_dir = os.path.join(settings.IMARIS_EXPORT_JOBS_DIR, 'logs')
        os.makedirs(scripts_dir, exist_ok=True)
        os.makedirs(logs_dir, exist_ok=True)
        manifest_path = root / 'mosaic.json'
        python_path = settings.IMARIS_EXPORT_PYTHON
        script = f'''#!/bin/bash
set -euo pipefail

PYIW_PYTHON={shlex.quote(python_path)}
PYIW_ENV_ROOT="$(dirname "$(dirname "$PYIW_PYTHON")")"
PYIW_SITE_PACKAGES="$("$PYIW_PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
export LD_LIBRARY_PATH="$PYIW_SITE_PACKAGES/PyImarisWriter:$PYIW_ENV_ROOT/lib:${{LD_LIBRARY_PATH:-}}"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
exec "$PYIW_PYTHON" -u -m virtual_mesospim.export_ims \\
    {shlex.quote(str(manifest_path))} {shlex.quote(str(output))} \\
    --reader-threads 8 --writer-threads 16 --max-prefetched-blocks 16
'''
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=scripts_dir,
                                         prefix=f'{job_name}_', suffix='.sbatch.sh',
                                         delete=False) as handle:
            handle.write(script)
            script_path = handle.name
        log_pattern = os.path.join(logs_dir, f'{job_name}_%j.log')
        submitted = subprocess.run(
            ['sbatch', '--parsable', '--partition=ai', '--cpus-per-task=32',
             '--mem=256G', f'--job-name={job_name}',
             f'--output={log_pattern}', f'--error={log_pattern}', script_path],
            capture_output=True, text=True)
        if submitted.returncode != 0:
            flash(f'Could not submit Imaris export: {submitted.stderr.strip()}', 'danger')
        else:
            job_id = submitted.stdout.strip().split(';', 1)[0]
            log_path = log_pattern.replace('%j', job_id)
            flash(f'Imaris export submitted as SLURM job {job_id}. '
                  f'Output: {output}. Log: {log_path}', 'success')
    except MosaicError as exc:
        flash(str(exc), 'danger')
    except OSError as exc:
        flash(f'Could not prepare or submit Imaris export: {exc}', 'danger')
    return redirect(url_for('edit_dataset', dataset_id=dataset_id))


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
        return redirect(url_for(modality_list_endpoint(new_dataset.modality)))

    return render_template('create_dataset.html', form=form)



if __name__ == '__main__':
    from server import main

    main(app)
