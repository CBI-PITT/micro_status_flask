import json
import re
import shutil
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import zarr
from PIL import Image

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
                array = group.create_dataset(str(i), shape=shape, dtype='uint16',
                                             chunks=(2, 8, 8))
                for z in range(shape[0]):
                    array[z, :, :] = (number + 1) * 100 + z * 10 * scale[0]
                levels.append({'path': str(i), 'coordinateTransformations': [
                    {'type': 'scale', 'scale': scale},
                    {'type': 'translation', 'translation': [0, y, x]},
                ]})
            group.attrs['multiscales'] = [{
                'version': '0.4',
                'axes': [{'name': axis, 'type': 'space'} for axis in 'zyx'],
                'datasets': levels,
            }]
    return tmp_path


def test_stage_geometry_and_shared_channel_manifest(dataset):
    assert len(mosaic.list_acquisitions(dataset)) == 1
    group, saved = mosaic.load_group(dataset, 0)
    assert saved is None
    assert (group['rows'], group['cols']) == (2, 2)
    assert [channel['name'] for channel in group['channels']] == ['Ch488', 'Ch561']
    assert group['stage_positions'] == [
        [0, 0, 0], [0, -12, 0], [0, 0, -12], [0, -12, -12]
    ]

    positions = [p.copy() for p in group['stage_positions']]
    positions[1][2] += 3
    path = mosaic.save_positions(dataset, group, positions)
    data = json.loads(path.read_text())
    assert data['positions_zyx'] == positions
    assert 'tiles' not in data
    assert [entry['name'] for entry in data['channels']] == ['Ch488', 'Ch561']
    assert all(len(entry['tiles']) == 4 for entry in data['channels'])
    assert all((dataset / tile).is_dir() for entry in data['channels'] for tile in entry['tiles'])
    assert mosaic.load_group(dataset, 0)[1]['positions_zyx'] == positions

    with pytest.raises(mosaic.MosaicError, match='z positions'):
        positions[1][0] = 1
        mosaic.save_positions(dataset, group, positions)


def test_render_coarse_first_and_select_z_and_contrast(dataset):
    group, _ = mosaic.load_group(dataset, 0)
    view = mosaic.view_data(dataset, group, None)
    assert view['z_count'] == 5
    assert view['tiles'][0]['levels'] == [
        {'width': 8, 'height': 8}, {'width': 16, 'height': 16}
    ]
    coarse = Image.open(BytesIO(mosaic.render_patch(group, 0, 0, 0, 0, 2, 0, 200)))
    fine = Image.open(BytesIO(mosaic.render_patch(group, 0, 1, 0, 0, 2, 0, 200)))
    assert coarse.size == (8, 8)
    assert fine.size == (16, 16)
    assert coarse.getpixel((0, 0)) == 153  # z=1 at 2x coarser scale => 120 / 200 * 255
    assert fine.getpixel((0, 0)) == 153
    assert Image.open(BytesIO(mosaic.render_patch(group, 0, 0, 0, 0, 2, 100, 200))).getpixel((0, 0)) == 51
    assert Image.open(BytesIO(mosaic.render_patch(group, 1, 0, 0, 0, 2, 0, 400))).getpixel((0, 0)) == 140
    with pytest.raises(mosaic.MosaicError, match='Z-plane'):
        mosaic.render_patch(group, 0, 0, 0, 0, 10, 0, 200)


def test_manifest_is_only_reused_for_matching_acquisition(dataset):
    group, _ = mosaic.load_group(dataset, 0)
    mosaic.save_positions(dataset, group, group['stage_positions'])
    path = dataset / 'mosaic.json'
    manifest = json.loads(path.read_text())
    manifest['channels'][1]['tiles'][0] = '../somewhere-else.ome.zarr'
    path.write_text(json.dumps(manifest))
    assert mosaic.load_group(dataset, 0)[1] is None


def test_single_channel_manifest(dataset):
    group, _ = mosaic.load_group(dataset, 0)
    group = {**group, 'channels': group['channels'][:1]}
    path = mosaic.save_positions(dataset, group, group['stage_positions'])
    manifest = json.loads(path.read_text())
    assert len(manifest['tiles']) == 4
    assert 'channels' not in manifest


def test_reference_manifest_loader_when_available(dataset):
    reference = pytest.importorskip('virtual_mesospim.config')
    group, _ = mosaic.load_group(dataset, 0)
    path = mosaic.save_positions(dataset, group, group['stage_positions'])
    configs = reference.load_mosaic_configs(path)
    assert [config.channel for config in configs] == ['Ch488', 'Ch561']
    assert configs[0].positions_zyx == tuple(map(tuple, group['stage_positions']))


def test_one_manifest_per_dataset_tracks_selected_acquisition(dataset):
    shutil.copytree(dataset / 'scan.ome.zarr', dataset / 'second.ome.zarr')
    first, _ = mosaic.load_group(dataset, 0)
    second, _ = mosaic.load_group(dataset, 1)
    mosaic.save_positions(dataset, first, first['stage_positions'])
    assert mosaic.load_group(dataset, 1)[1] is None
    mosaic.save_positions(dataset, second, second['stage_positions'])
    assert mosaic.load_group(dataset, 0)[1] is None
    assert mosaic.load_group(dataset, 1)[1] is not None


def test_viewer_routes_and_authenticated_save(dataset, monkeypatch):
    pytest.importorskip('flask_sqlalchemy')
    import app as app_module

    monkeypatch.setattr(app_module, 'mosaic_dataset', lambda dataset_id: SimpleNamespace(
        id=dataset_id, name='Test dataset', modality='mesospim',
        path_on_fast_store=str(dataset), path_on_hive=None, moved=False
    ))
    monkeypatch.setitem(app_module.app.config, 'TESTING', True)
    monkeypatch.setitem(app_module.app.config, 'SESSION_COOKIE_SECURE', False)
    client = app_module.app.test_client()
    assert client.get('/datasets/1/mosaic').status_code == 302
    with client.session_transaction() as login_session:
        login_session['_user_id'] = 'CBI_Admin'
        login_session['_fresh'] = True

    response = client.get('/datasets/1/mosaic')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    match = re.search(r'<script id="mosaic-config" type="application/json">(.*?)</script>',
                      html, re.S)
    config = json.loads(match.group(1))
    assert config['data']['rows'] == 2
    assert config['data']['channels'] == ['Ch488', 'Ch561']
    image = client.get('/datasets/1/mosaic/tiles/0/0/0/0/0.png?z=2&lo=0&hi=200')
    assert image.status_code == 200
    assert Image.open(BytesIO(image.data)).size == (8, 8)
    assert client.get('/datasets/1/mosaic/tiles/0/0/0/0/0.png?z=10&lo=0&hi=200').status_code == 400

    save_url = '/datasets/1/mosaic/positions/0'
    positions = config['data']['positions_zyx']
    rejected = client.post(save_url, json={'positions_zyx': positions})
    assert rejected.status_code == 403
    assert rejected.is_json
    assert 'authorization' in rejected.json['error'].lower()
    response = client.post(save_url, json={'positions_zyx': positions},
                           headers={'X-CSRF-Token': config['csrf_token']})
    assert response.status_code == 200
    assert (dataset / 'mosaic.json').exists()
    assert client.get('/datasets/1/mosaic').status_code == 200


def test_http_save_with_remember_cookie_and_no_session_csrf(dataset, monkeypatch):
    pytest.importorskip('flask_sqlalchemy')
    from flask_login.utils import encode_cookie
    import app as app_module

    monkeypatch.setattr(app_module, 'mosaic_dataset', lambda dataset_id: SimpleNamespace(
        id=dataset_id, name='Test dataset', modality='mesospim',
        path_on_fast_store=str(dataset), path_on_hive=None, moved=False
    ))
    monkeypatch.setitem(app_module.app.config, 'TESTING', True)
    monkeypatch.setitem(app_module.app.config, 'SESSION_COOKIE_SECURE', True)
    client = app_module.app.test_client()
    client.set_cookie('remember_token', encode_cookie('CBI_Admin', app_module.app.secret_key))

    response = client.get('/datasets/1/mosaic', base_url='http://localhost')
    assert response.status_code == 200
    match = re.search(r'<script id="mosaic-config" type="application/json">(.*?)</script>',
                      response.get_data(as_text=True), re.S)
    config = json.loads(match.group(1))
    with client.session_transaction() as login_session:
        assert 'mosaic_csrf' not in login_session

    save_url = '/datasets/1/mosaic/positions/0'
    payload = {'positions_zyx': config['data']['positions_zyx']}
    wrong_token = app_module.mosaic_token_serializer().dumps(['CBI_Admin', 2, 0])
    rejected = client.post(save_url, json=payload,
                           headers={'X-CSRF-Token': wrong_token}, base_url='http://localhost')
    assert rejected.status_code == 403
    assert rejected.is_json
    assert not (dataset / 'mosaic.json').exists()

    response = client.post(save_url, json=payload,
                           headers={'X-CSRF-Token': config['csrf_token']},
                           base_url='http://localhost')
    assert response.status_code == 200
    assert (dataset / 'mosaic.json').exists()
