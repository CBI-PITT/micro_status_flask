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
