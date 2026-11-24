import asyncio
import base64
import json
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
import zarr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'micro_status_flask'))
import mosaic


@pytest.fixture
def dataset(tmp_path):
    acquisition = tmp_path / 'scan.ome.zarr'
    acquisition.mkdir()
    for channel in ('488', '561'):
        for number in range(4):
            tile = acquisition / f'Mag8_Tile{number}_Ch{channel}_Sh0.ome.zarr'
            try:
                group = zarr.open_group(str(tile), mode='w', zarr_format=2)
            except TypeError:
                group = zarr.open_group(str(tile), mode='w')
            y, x = -12 * (number % 2), -12 * (number // 2)
            levels = []
            for i, (shape, scale) in enumerate((((5, 16, 16), (1, 1, 1)),
                                                 ((3, 8, 8), (2, 2, 2)))):
                create_array = (group.create_dataset if hasattr(group, 'create_dataset')
                                else group.create_array)
                array = create_array(str(i), shape=shape, dtype='uint16',
                                     chunks=(2, 8, 8))
                for z in range(shape[0]):
                    array[z, :, :] = (number + 1) * 100 + z * 10 * scale[0]
                levels.append({'path': str(i), 'coordinateTransformations': [
                    {'type': 'scale', 'scale': list(scale)},
                    {'type': 'translation', 'translation': [0, y, x]},
                ]})
            group.attrs['multiscales'] = [{
                'version': '0.4',
                'axes': [{'name': axis, 'type': 'space'} for axis in 'zyx'],
                'datasets': levels,
            }]
    return tmp_path


class _FakeQuery:
    def __init__(self, item):
        self._item = item

    def get_or_404(self, dataset_id):
        return self._item

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return [SimpleNamespace(id=3, name='Doe')]


@pytest.fixture
def app_client(dataset, monkeypatch):
    pytest.importorskip('flask_sqlalchemy')
    import app as app_module

    stub = SimpleNamespace(id=1, name='Test dataset', modality='mesospim',
                           path_on_fast_store=str(dataset), path_on_hive=None,
                           moved=False)
    monkeypatch.setattr(app_module, 'mosaic_dataset', lambda dataset_id: stub)
    monkeypatch.setattr(app_module, 'Dataset', SimpleNamespace(query=_FakeQuery(stub)))
    monkeypatch.setattr(app_module, 'PI',
                        SimpleNamespace(query=_FakeQuery(None), name='name'))
    monkeypatch.setattr(app_module.settings, 'VIRTUAL_ZARR_SERVICE_PATH', '/virtual-zarr')
    monkeypatch.setitem(app_module.app.config, 'TESTING', True)
    monkeypatch.setitem(app_module.app.config, 'SESSION_COOKIE_SECURE', False)
    return app_module, app_module.app.test_client()


def _login(client):
    with client.session_transaction() as login_session:
        login_session['_user_id'] = 'CBI_Admin'
        login_session['_fresh'] = True


def test_edit_page_shows_button(app_client):
    app_module, client = app_client
    _login(client)
    html = client.get('/datasets/1/edit').get_data(as_text=True)
    assert 'View virtually stitched tiles' in html
    match = re.search(r'name="token" value="([^"]+)"', html)
    assert match
    assert app_module.virtual_zarr_token_serializer().loads(match.group(1)) == ['CBI_Admin', 1]


def test_open_requires_valid_token(app_client):
    app_module, client = app_client
    _login(client)
    url = '/datasets/1/virtual_stitched'
    assert client.post(url).status_code == 403
    assert client.post(url, data={'token': 'not-a-token'}).status_code == 403
    wrong_dataset = app_module.virtual_zarr_token_serializer().dumps(['CBI_Admin', 2])
    assert client.post(url, data={'token': wrong_dataset}).status_code == 403
    wrong_user = app_module.virtual_zarr_token_serializer().dumps(['Someone', 1])
    assert client.post(url, data={'token': wrong_user}).status_code == 403


def test_open_creates_manifest_and_redirects(app_client, dataset):
    app_module, client = app_client
    _login(client)
    response = client.post(
        '/datasets/1/virtual_stitched',
        data={'token': app_module.virtual_zarr_token_serializer().dumps(['CBI_Admin', 1])})
    assert response.status_code == 302
    location = response.headers['Location']
    parsed = urlparse(location)
    assert parsed.path == '/virtual-zarr/datasets/1/viewer'
    assert not parsed.netloc
    token = parse_qs(parsed.query)['token'][0]
    encoded, _ = token.split('.', 1)
    dataset_id, expiry, path = base64.urlsafe_b64decode(encoded.encode()).decode().split('|', 2)
    assert dataset_id == '1'
    assert path == str(dataset)
    assert int(expiry) > time.time()

    group, _ = mosaic.load_group(dataset, 0)
    manifest = json.loads((dataset / 'mosaic.json').read_text())
    assert manifest['positions_zyx'] == group['stage_positions']
    assert [entry['name'] for entry in manifest['channels']] == ['Ch488', 'Ch561']


def test_flask_link_opens_mounted_viewer_and_serves_chunks(app_client):
    from server import create_asgi_app

    app_module, flask_client = app_client
    _login(flask_client)
    form_token = app_module.virtual_zarr_token_serializer().dumps(['CBI_Admin', 1])
    launched = flask_client.post('/datasets/1/virtual_stitched',
                                 data={'token': form_token})
    assert launched.status_code == 302

    async def check():
        transport = httpx.ASGITransport(app=create_asgi_app(app_module.app))
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            viewer = await client.get(launched.headers['Location'])
            assert viewer.status_code == 302
            assert viewer.headers['location'].startswith('/virtual-zarr/v/viewer/')
            state = json.loads(unquote(viewer.headers['location'].split('#!', 1)[1]))
            source = state['layers'][0]['source']
            assert source.startswith('http://test/virtual-zarr/t/')
            store = source.removesuffix('/|zarr3:')
            metadata = await client.get(f'{store}/zarr.json')
            assert metadata.status_code == 200
            assert metadata.json()['zarr_format'] == 3
            assert (await client.get(f'{store}/0/c/0/0/0')).status_code == 200

    asyncio.run(check())


def test_open_reuses_matching_manifest(app_client, dataset):
    app_module, client = app_client
    group, _ = mosaic.load_group(dataset, 0)
    positions = [position.copy() for position in group['stage_positions']]
    positions[1][1] += 5
    mosaic.save_positions(dataset, group, positions)
    before = (dataset / 'mosaic.json').read_text()
    _login(client)
    client.post('/datasets/1/virtual_stitched',
                data={'token': app_module.virtual_zarr_token_serializer().dumps(['CBI_Admin', 1])})
    assert (dataset / 'mosaic.json').read_text() == before


def test_open_uses_manifest_acquisition(app_client, dataset):
    app_module, client = app_client
    shutil.copytree(dataset / 'scan.ome.zarr', dataset / 'second.ome.zarr')
    second, _ = mosaic.load_group(dataset, 1)
    second_positions = [position.copy() for position in second['stage_positions']]
    second_positions[2][2] += 7
    mosaic.save_positions(dataset, second, second_positions)
    _login(client)
    client.post('/datasets/1/virtual_stitched',
                data={'token': app_module.virtual_zarr_token_serializer().dumps(['CBI_Admin', 1])})
    manifest = json.loads((dataset / 'mosaic.json').read_text())
    assert manifest['positions_zyx'] == second_positions


def test_open_without_tiles_redirects_with_flash(tmp_path, monkeypatch):
    pytest.importorskip('flask_sqlalchemy')
    import app as app_module

    empty = tmp_path / 'empty'
    empty.mkdir()
    stub = SimpleNamespace(id=1, name='Test dataset', modality='mesospim',
                           path_on_fast_store=str(empty), path_on_hive=None, moved=False)
    monkeypatch.setattr(app_module, 'mosaic_dataset', lambda dataset_id: stub)
    monkeypatch.setattr(app_module, 'Dataset', SimpleNamespace(query=_FakeQuery(stub)))
    monkeypatch.setattr(app_module, 'PI',
                        SimpleNamespace(query=_FakeQuery(None), name='name'))
    monkeypatch.setattr(app_module.settings, 'VIRTUAL_ZARR_SERVICE_PATH', '/virtual-zarr')
    monkeypatch.setitem(app_module.app.config, 'TESTING', True)
    monkeypatch.setitem(app_module.app.config, 'SESSION_COOKIE_SECURE', False)
    client = app_module.app.test_client()
    _login(client)
    response = client.post(
        '/datasets/1/virtual_stitched',
        data={'token': app_module.virtual_zarr_token_serializer().dumps(['CBI_Admin', 1])})
    assert response.status_code == 302
    assert '/datasets/1/edit' in response.headers['Location']
    html = client.get('/datasets/1/edit').get_data(as_text=True)
    assert 'No MesoSPIM OME-Zarr' in html


def test_open_non_mesospim_404(dataset, monkeypatch):
    pytest.importorskip('flask_sqlalchemy')
    import app as app_module

    def not_found(dataset_id):
        from werkzeug.exceptions import abort
        abort(404)

    stub = SimpleNamespace(id=1, name='Test dataset', modality='rscm',
                           path_on_fast_store=str(dataset), path_on_hive=None, moved=False)
    monkeypatch.setattr(app_module, 'mosaic_dataset', not_found)
    monkeypatch.setattr(app_module.settings, 'VIRTUAL_ZARR_SERVICE_PATH', '/virtual-zarr')
    monkeypatch.setitem(app_module.app.config, 'TESTING', True)
    monkeypatch.setitem(app_module.app.config, 'SESSION_COOKIE_SECURE', False)
    client = app_module.app.test_client()
    _login(client)
    response = client.post(
        '/datasets/1/virtual_stitched',
        data={'token': app_module.virtual_zarr_token_serializer().dumps(['CBI_Admin', 1])})
    assert response.status_code == 404
