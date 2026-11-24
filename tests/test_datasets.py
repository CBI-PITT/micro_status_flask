import shlex
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'micro_status_flask'))

DAY_FORMAT = '%Y-%m-%d_%H-%M-%S'

# Values for the NOT NULL form fields so a form post validates. All other
# fields are optional and are set to None by populate_obj().
FORM_BASE_DATA = {
    'imaging_status': 'in_progress',
    'processing_status': 'not_started',
    'channels': '1',
    'keep_composites': '0',
    'delete_405': '0',
    'is_brain': '0',
    'peace_json_created': '0',
    'moved': '0',
    'moving': '0',
    'paused': '0',
    'public': '0',
}


@pytest.fixture(scope='session')
def database_path(tmp_path_factory):
    return tmp_path_factory.mktemp('dataset_list') / 'datasets.db'


@pytest.fixture
def app_client(database_path, monkeypatch):
    pytest.importorskip('flask_sqlalchemy')
    import sqlalchemy as sa
    import app as app_module
    from models import db

    monkeypatch.setitem(app_module.app.config, 'TESTING', True)
    monkeypatch.setitem(app_module.app.config, 'SESSION_COOKIE_SECURE', False)
    # The engine is created at init_app against the production path, so it is
    # swapped for one bound to a temporary file for the duration of the test.
    engine = sa.create_engine(f'sqlite:///{database_path}')
    monkeypatch.setitem(db._app_engines[app_module.app], None, engine)
    with app_module.app.app_context():
        db.create_all()
        yield app_module, app_module.app.test_client()
        db.session.remove()
        db.drop_all()


def add_pi(name):
    from models import PI, db

    pi = PI(name=name)
    db.session.add(pi)
    db.session.flush()
    return pi


def add_cl_number(name):
    from models import CLNumber, db

    cl_number = CLNumber(name=name)
    db.session.add(cl_number)
    db.session.flush()
    return cl_number


def add_dataset(**kwargs):
    from models import Dataset, db

    values = dict(
        name='ds',
        path_on_fast_store='/CBI_FastStore/Acquire/x',
        modality='mesospim',
        imaging_status='in_progress',
        processing_status='not_started',
        created=datetime.now().strftime(DAY_FORMAT),
    )
    values.update(kwargs)
    dataset = Dataset(**values)
    db.session.add(dataset)
    return dataset


def login(client, user_id='CBI_Admin'):
    with client.session_transaction() as login_session:
        login_session['_user_id'] = user_id
        login_session['_fresh'] = True


def commit():
    from models import db

    db.session.commit()


def get_dataset(dataset_id):
    from models import Dataset, db

    return db.session.get(Dataset, dataset_id)


def test_root_page_lists_mesospim_by_default(app_client):
    app_module, client = app_client
    with app_module.app.app_context():
        add_dataset(name='meso-ds',
                    path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/meso-ds')
        add_dataset(name='rscm-ds', modality='rscm',
                    path_on_fast_store='/CBI_FastStore/Acquire/RSCM/rscm-ds')
        commit()
    login(client)
    html = client.get('/').get_data(as_text=True)
    assert 'MesoSPIM Acquisition Data' in html
    assert 'MesoSPIM/meso-ds' in html
    assert 'RSCM/rscm-ds' not in html


def test_rscm_page_lists_rscm(app_client):
    app_module, client = app_client
    with app_module.app.app_context():
        add_dataset(name='meso-ds',
                    path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/meso-ds')
        add_dataset(name='rscm-ds', modality='rscm',
                    path_on_fast_store='/CBI_FastStore/Acquire/RSCM/rscm-ds')
        commit()
    login(client)
    html = client.get('/rscm').get_data(as_text=True)
    assert 'RSCM Acquisition Data' in html
    assert 'RSCM/rscm-ds' in html
    assert 'MesoSPIM/meso-ds' not in html


def test_time_filter_on_mesospim_page(app_client):
    app_module, client = app_client
    with app_module.app.app_context():
        add_dataset(path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/fresh-ds',
                    created=datetime.now().strftime(DAY_FORMAT))
        add_dataset(path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/old-ds',
                    created=(datetime.now() - timedelta(days=60)).strftime(DAY_FORMAT))
        commit()
    login(client)
    html = client.get('/?time=month').get_data(as_text=True)
    assert 'MesoSPIM/fresh-ds' in html
    assert 'MesoSPIM/old-ds' not in html
    html = client.get('/?time=all').get_data(as_text=True)
    assert 'MesoSPIM/old-ds' in html


def test_datasets_sorted_most_recent_first(app_client):
    app_module, client = app_client
    now = datetime.now()
    with app_module.app.app_context():
        add_dataset(path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/old-ds',
                    created=(now - timedelta(days=60)).strftime(DAY_FORMAT))
        add_dataset(path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/mid-ds',
                    created=(now - timedelta(days=10)).strftime(DAY_FORMAT))
        add_dataset(path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/new-ds',
                    created=now.strftime(DAY_FORMAT))
        add_dataset(path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/no-date-ds',
                    created=None)
        commit()
    login(client)
    html = client.get('/?time=all').get_data(as_text=True)
    assert html.index('MesoSPIM/new-ds') < html.index('MesoSPIM/mid-ds')
    assert html.index('MesoSPIM/mid-ds') < html.index('MesoSPIM/old-ds')
    assert html.index('MesoSPIM/old-ds') < html.index('MesoSPIM/no-date-ds')


def test_admin_pi_filter(app_client):
    app_module, client = app_client
    with app_module.app.app_context():
        pi_alpha = add_pi('Alpha')
        pi_beta = add_pi('Beta')
        add_dataset(pi=pi_alpha.id,
                    path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/alpha-ds')
        add_dataset(pi=pi_beta.id,
                    path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/beta-ds')
        commit()
        alpha_id = pi_alpha.id
    login(client)
    html = client.get(f'/?name={alpha_id}').get_data(as_text=True)
    assert 'MesoSPIM/alpha-ds' in html
    assert 'MesoSPIM/beta-ds' not in html


def test_non_admin_sees_only_own_pi(app_client):
    app_module, client = app_client
    with app_module.app.app_context():
        pi_alpha = add_pi('Alpha')
        pi_beta = add_pi('Beta')
        add_dataset(pi=pi_alpha.id,
                    path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/alpha-ds')
        add_dataset(modality='rscm', pi=pi_beta.id,
                    path_on_fast_store='/CBI_FastStore/Acquire/RSCM/beta-ds')
        commit()
    login(client, user_id='Alpha')
    html = client.get('/').get_data(as_text=True)
    assert 'MesoSPIM/alpha-ds' in html
    assert 'RSCM/beta-ds' not in html
    html = client.get('/rscm').get_data(as_text=True)
    assert 'MesoSPIM/alpha-ds' not in html


def test_navigation_links_carry_filters(app_client):
    app_module, client = app_client
    login(client)
    html = client.get('/?time=month').get_data(as_text=True)
    assert 'href="/rscm?time=month"' in html
    html = client.get('/rscm?time=month').get_data(as_text=True)
    assert 'href="/?time=month"' in html


def test_legacy_datasets_route_removed(app_client):
    app_module, client = app_client
    login(client)
    assert client.get('/datasets').status_code == 404


def test_edit_redirects_to_modality_page(app_client):
    app_module, client = app_client
    with app_module.app.app_context():
        pi = add_pi('Alpha')
        meso = add_dataset(name='meso-ds')
        rscm = add_dataset(name='rscm-ds', modality='rscm')
        commit()
        meso_id, rscm_id, pi_id = meso.id, rscm.id, pi.id
    login(client)
    response = client.post(f'/datasets/{meso_id}/edit',
                           data={**FORM_BASE_DATA, 'pi': str(pi_id),
                                 'modality': 'mesospim'})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/')
    response = client.post(f'/datasets/{rscm_id}/edit',
                           data={**FORM_BASE_DATA, 'pi': str(pi_id),
                                 'modality': 'rscm'})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/rscm')


def test_edit_page_field_layout(app_client):
    app_module, client = app_client
    with app_module.app.app_context():
        pi = add_pi('Alpha')
        cl = add_cl_number('123')
        add_dataset(cl_number=cl.id, pi=pi.id,
                    path_on_fast_store='/CBI_FastStore/Acquire/MesoSPIM/meso-ds')
        add_dataset(modality='rscm', pi=pi.id,
                    path_on_fast_store='/CBI_FastStore/Acquire/RSCM/rscm-ds')
        commit()
    login(client)
    meso_html = client.get('/datasets/1/edit').get_data(as_text=True)
    rscm_html = client.get('/datasets/2/edit').get_data(as_text=True)

    # Common fields appear in the requested order
    order = ['Path on Fast Store', 'Path on Hive', 'Imaris File Path', 'Name',
             'CL Number', 'Imaging Status', 'Processing Status', 'Moved']
    positions = [meso_html.index(label) for label in order]
    assert positions == sorted(positions)

    # CL number dropdown shows the actual number, not the database id
    assert '-- None --' in meso_html
    assert '123' in meso_html

    # RSCM-specific and unlisted fields are hidden on the MesoSPIM page
    for absent in ['Ribbons Total', 'Ribbons Finished', 'Z Layers Checked',
                   'Keep Composites', 'Delete 405', 'Channels', 'Tiles Total',
                   'Modality', 'Job Number']:
        assert absent not in meso_html

    # Created is shown read-only
    assert 'Created' in meso_html

    # RSCM-specific fields appear for the RSCM dataset
    assert 'Ribbons Total' in rscm_html
    assert 'Keep Composites' in rscm_html
    assert 'Delete 405' in rscm_html


def test_edit_save_preserves_hidden_fields(app_client):
    app_module, client = app_client
    with app_module.app.app_context():
        pi = add_pi('Alpha')
        ds = add_dataset(pi=pi.id, channels=3, job_number='J1',
                         resolution_xy='1.1x1.1', created='2026-01-01_00-00-00')
        commit()
        ds_id, pi_id = ds.id, pi.id
    login(client)
    response = client.post(f'/datasets/{ds_id}/edit',
                           data={**FORM_BASE_DATA, 'pi': str(pi_id)})
    assert response.status_code == 302
    with app_module.app.app_context():
        dataset = get_dataset(ds_id)
        assert dataset.channels == 3
        assert dataset.job_number == 'J1'
        assert dataset.resolution_xy == '1.1x1.1'
        assert dataset.created == '2026-01-01_00-00-00'
        assert dataset.modality == 'mesospim'


def test_create_redirects_to_modality_page(app_client):
    app_module, client = app_client
    with app_module.app.app_context():
        pi = add_pi('Alpha')
        cl_number = add_cl_number('123')
        commit()
        pi_id, cl_id = pi.id, cl_number.id
    login(client)
    data = {**FORM_BASE_DATA, 'name': 'new-rscm-ds', 'modality': 'rscm',
            'pi': str(pi_id), 'cl_number': str(cl_id)}
    response = client.post('/datasets/new', data=data)
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/rscm')


def test_imaris_export_submits_quoted_job_and_leaves_database_unchanged(
        app_client, monkeypatch, tmp_path):
    app_module, client = app_client
    root = tmp_path / "data with 'quote'"
    root.mkdir()
    (root / 'mosaic.json').write_text('{}')
    with app_module.app.app_context():
        ds = add_dataset(path_on_fast_store=str(root), imaris_file_path='existing.ims')
        commit()
        dataset_id = ds.id
    monkeypatch.setattr(app_module.settings, 'IMARIS_EXPORT_JOBS_DIR', str(tmp_path / 'jobs'))
    monkeypatch.setattr(app_module, 'prepare_virtual_mosaic',
                        lambda dataset: (root, {'name': 'scan.ome.zarr'}))
    monkeypatch.setattr(app_module, 'list_acquisitions', lambda root: [root / 'scan.ome.zarr'])
    calls = []
    run_shell = subprocess.run

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == 'squeue':
            return subprocess.CompletedProcess(args, 0, '', '')
        assert args[0] == 'sbatch'
        return subprocess.CompletedProcess(args, 0, '12345\n', '')

    monkeypatch.setattr(app_module.subprocess, 'run', run)
    login(client)
    edit_html = client.get(f'/datasets/{dataset_id}/edit').get_data(as_text=True)
    assert 'Export to Imaris' in edit_html
    assert f'/datasets/{dataset_id}/export_imaris' in edit_html
    token = app_module.imaris_export_token_serializer().dumps(['CBI_Admin', dataset_id])
    response = client.post(f'/datasets/{dataset_id}/export_imaris',
                           data={'token': token}, follow_redirects=True)
    assert response.status_code == 200
    assert 'SLURM job 12345' in response.get_data(as_text=True)
    assert 'scan.ims' in response.get_data(as_text=True)
    assert calls[0][0][0] == 'squeue'
    sbatch_args = calls[1][0]
    assert {'--parsable', '--partition=ai', '--cpus-per-task=32', '--mem=256G',
            f'--job-name=imaris_{dataset_id:05d}'}.issubset(sbatch_args)
    assert any(arg.endswith(f'/logs/imaris_{dataset_id:05d}_%j.log')
               for arg in sbatch_args if arg.startswith('--output='))
    script = Path(sbatch_args[-1]).read_text()
    assert run_shell(['/bin/bash', '-n', sbatch_args[-1]], capture_output=True).returncode == 0
    assert f'PYIW_PYTHON={shlex.quote(app_module.settings.IMARIS_EXPORT_PYTHON)}' in script
    assert 'PyImarisWriter:$PYIW_ENV_ROOT/lib' in script
    assert '-u -m virtual_mesospim.export_ims' in script
    assert shlex.quote(str(root / 'mosaic.json')) in script
    assert shlex.quote(str(root / 'virtual_ims' / 'scan.ims')) in script
    assert '--reader-threads 8 --writer-threads 16 --max-prefetched-blocks 16' in script
    assert not (root / 'virtual_ims' / 'scan.ims').exists()
    with app_module.app.app_context():
        assert get_dataset(dataset_id).imaris_file_path == 'existing.ims'


def test_imaris_export_requires_login(app_client):
    app_module, client = app_client
    with app_module.app.app_context():
        ds = add_dataset()
        commit()
        dataset_id = ds.id
    url = f'/datasets/{dataset_id}/export_imaris'
    assert client.get(url).status_code == 405
    assert client.post(url).status_code == 302


def test_imaris_export_requires_auth_token_and_mesospim(app_client, monkeypatch):
    app_module, client = app_client
    with app_module.app.app_context():
        meso = add_dataset()
        rscm = add_dataset(modality='rscm')
        commit()
        meso_id, rscm_id = meso.id, rscm.id
    url = f'/datasets/{meso_id}/export_imaris'
    login(client)
    assert client.post(url).status_code == 403
    assert client.post(url, data={'token': 'invalid'}).status_code == 403
    wrong_user = app_module.imaris_export_token_serializer().dumps(['Other', meso_id])
    wrong_dataset = app_module.imaris_export_token_serializer().dumps(['CBI_Admin', rscm_id])
    viewer_token = app_module.virtual_zarr_token_serializer().dumps(['CBI_Admin', meso_id])
    for token in (wrong_user, wrong_dataset, viewer_token):
        assert client.post(url, data={'token': token}).status_code == 403
    rscm_token = app_module.imaris_export_token_serializer().dumps(['CBI_Admin', rscm_id])
    assert client.post(f'/datasets/{rscm_id}/export_imaris',
                       data={'token': rscm_token}).status_code == 404
    monkeypatch.setattr(app_module, 'list_acquisitions', lambda root: [root])
    assert 'Export to Imaris' not in client.get(f'/datasets/{rscm_id}/edit').get_data(as_text=True)


@pytest.mark.parametrize('case,expected', [
    ('existing', 'already exists'),
    ('queued', 'already queued or running'),
    ('queue_error', 'Could not check Imaris export jobs'),
    ('submit_error', 'Could not submit Imaris export'),
])
def test_imaris_export_does_not_duplicate_or_hide_submission_failure(
        app_client, monkeypatch, tmp_path, case, expected):
    app_module, client = app_client
    root = tmp_path / 'dataset'
    root.mkdir()
    with app_module.app.app_context():
        ds = add_dataset(path_on_fast_store=str(root))
        commit()
        dataset_id = ds.id
    monkeypatch.setattr(app_module.settings, 'IMARIS_EXPORT_JOBS_DIR', str(tmp_path / 'jobs'))
    monkeypatch.setattr(app_module, 'prepare_virtual_mosaic',
                        lambda dataset: (root, {'name': 'scan.ome.zarr'}))
    if case == 'existing':
        output = root / 'virtual_ims'
        output.mkdir()
        (output / 'scan.ims').write_bytes(b'existing')
    calls = []

    def run(args, **kwargs):
        calls.append(args[0])
        if args[0] == 'squeue':
            if case == 'queued':
                return subprocess.CompletedProcess(args, 0, '345\n', '')
            if case == 'queue_error':
                return subprocess.CompletedProcess(args, 1, '', 'scheduler unavailable')
            return subprocess.CompletedProcess(args, 0, '', '')
        return subprocess.CompletedProcess(args, 1, '', 'submission rejected')

    monkeypatch.setattr(app_module.subprocess, 'run', run)
    login(client)
    token = app_module.imaris_export_token_serializer().dumps(['CBI_Admin', dataset_id])
    response = client.post(f'/datasets/{dataset_id}/export_imaris',
                           data={'token': token}, follow_redirects=True)
    assert expected in response.get_data(as_text=True)
    assert calls == (['squeue', 'sbatch'] if case == 'submit_error' else
                     [] if case == 'existing' else ['squeue'])


def test_imaris_export_without_tiles_reports_error(app_client, monkeypatch, tmp_path):
    app_module, client = app_client
    empty = tmp_path / 'empty'
    empty.mkdir()
    with app_module.app.app_context():
        ds = add_dataset(path_on_fast_store=str(empty))
        commit()
        dataset_id = ds.id
    monkeypatch.setattr(app_module.subprocess, 'run',
                        lambda *args, **kwargs: pytest.fail('SLURM should not be called'))
    login(client)
    token = app_module.imaris_export_token_serializer().dumps(['CBI_Admin', dataset_id])
    response = client.post(f'/datasets/{dataset_id}/export_imaris',
                           data={'token': token}, follow_redirects=True)
    assert 'No MesoSPIM OME-Zarr' in response.get_data(as_text=True)
