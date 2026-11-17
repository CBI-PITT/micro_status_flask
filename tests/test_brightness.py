import json
import re
import shutil
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import zarr
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'micro_status_flask'))
import mosaic


def _build_dataset(root, short_tiles=(), y_sign=-1, x_sign=-1):
    acquisition = root / 'scan.ome.zarr'
    acquisition.mkdir()
    for channel, offset in (('488', 0), ('561', 50)):
        for number in range(4):
            tile = acquisition / f'Mag8_Tile{number}_Ch{channel}_Sh0.ome.zarr'
            try:
                group = zarr.open_group(str(tile), mode='w', zarr_format=2)
            except TypeError:
                group = zarr.open_group(str(tile), mode='w')
            z_planes = 4 if number in short_tiles else 5
            y, x = y_sign * 12 * (number % 2), x_sign * 12 * (number // 2)
            levels = []
            for i, (shape, scale) in enumerate((((z_planes, 16, 16), (1, 1, 1)),
                                                 (((z_planes + 1) // 2, 8, 8), (2, 2, 2)))):
                array = group.create_dataset(str(i), shape=shape, dtype='uint16',
                                             chunks=(2, 8, 8))
                for z in range(shape[0]):
                    array[z, :, :] = (number + 1) * 100 + offset + z * 10 * scale[0]
                levels.append({'path': str(i), 'coordinateTransformations': [
                    {'type': 'scale', 'scale': scale},
                    {'type': 'translation', 'translation': [0, y, x]},
                ]})
            group.attrs['multiscales'] = [{
                'version': '0.4',
                'axes': [{'name': axis, 'type': 'space'} for axis in 'zyx'],
                'datasets': levels,
            }]
            meta = acquisition.parent / f'{acquisition.name}_{tile.name}_meta.txt'
            meta.write_text(
                '[Metadata for file]\n[CFG] \n[Laser] 561 nm\n'
                f'[Shutter] {"Left" if number % 2 == 0 else "Right"}\n',
                encoding='utf-8',
            )
    return root


@pytest.fixture
def dataset(tmp_path):
    return _build_dataset(tmp_path)


@pytest.fixture
def short_dataset(tmp_path):
    return _build_dataset(tmp_path, short_tiles=(2,))


def test_arms_read_from_meta_files(dataset):
    group, _ = mosaic.load_group(dataset, 0)
    view = mosaic.brightness_data(group, None, None)
    assert view['arms'] == ['L', 'R', 'L', 'R']
    # Only the first channel's meta files have been read so far.
    (dataset / 'scan.ome.zarr_Mag8_Tile2_Ch561_Sh0.ome.zarr_meta.txt').unlink()
    assert mosaic._tile_arm(group['channels'][1]['tiles'][2]) == '?'


def test_brightness_view_defaults(dataset):
    group, _ = mosaic.load_group(dataset, 0)
    view = mosaic.brightness_data(group, None, None)
    assert view['channels'] == ['Ch488', 'Ch561']
    assert view['gains'] == [[1.0] * 4, [1.0] * 4]
    assert view['gain_limits'] == [mosaic.GAIN_MIN, mosaic.GAIN_MAX]
    assert view['z_count'] == 5
    assert (view['rows'], view['cols']) == (2, 2)


def test_tiles_with_different_z_counts_load_and_render(short_dataset):
    group, _ = mosaic.load_group(short_dataset, 0)
    assert (group['rows'], group['cols']) == (2, 2)
    view = mosaic.brightness_data(group, None, None)
    assert view['z_count'] == 5
    assert view['arms'] == ['L', 'R', 'L', 'R']
    assert mosaic.load_group(short_dataset, 0)[1] is None
    gains = [{'name': 'Ch488', 'gains': [1.0, 1.1, 0.9, 1.0]},
             {'name': 'Ch561', 'gains': [1.0] * 4}]
    mosaic.save_gains(short_dataset, group, gains)
    assert mosaic.load_gains(short_dataset, group)['channels'][0]['gains'] == gains[0]['gains']

    # Tile 2 has 4 planes: base_z=4 renders black for it, normally for tile 0.
    black = Image.open(BytesIO(mosaic.render_patch(group, 2, 0, 0, 0, 4, 0, 500)))
    assert black.getextrema() == (0, 0)
    last = Image.open(BytesIO(mosaic.render_patch(group, 0, 0, 0, 0, 4, 0, 500)))
    assert last.getpixel((0, 0)) == (100 + 40) * 255 // 500
    with pytest.raises(mosaic.MosaicError, match='Z-plane'):
        mosaic.render_patch(group, 0, 0, 0, 0, 5, 0, 500)


def test_rejects_tiles_with_different_yx_sizes(tmp_path):
    _build_dataset(tmp_path)
    tile = tmp_path / 'scan.ome.zarr' / 'Mag8_Tile3_Ch488_Sh0.ome.zarr'
    for level, shape in (('0', (5, 12, 16)), ('1', (3, 6, 8))):
        array = zarr.open_array(str(tile / level), mode='r+')
        array.resize(shape)
    with pytest.raises(mosaic.MosaicError, match='Tile sizes'):
        mosaic.load_group(tmp_path, 0)


def test_stage_positions_canonicalized_regardless_of_sweep_direction(tmp_path):
    # Like the real 111226 dataset (+y rows, -x columns), plus both-increasing
    # and y-decreasing sweeps: display positions always grow down and right.
    for y_sign, x_sign in ((1, -1), (1, 1), (-1, 1)):
        root = tmp_path / f'ds_{y_sign}_{x_sign}'
        root.mkdir()
        _build_dataset(root, y_sign=y_sign, x_sign=x_sign)
        group, _ = mosaic.load_group(root, 0)
        assert (group['rows'], group['cols']) == (2, 2)
        assert group['stage_positions'] == [
            [0, 0, 0], [0, 12, 0], [0, 0, 12], [0, 12, 12]
        ]


def test_gain_save_and_load_roundtrip(dataset):
    group, _ = mosaic.load_group(dataset, 0)
    assert mosaic.load_gains(dataset, group) is None
    gains = [{'name': 'Ch488', 'gains': [1.0, 1.1, 0.9, 2.0]},
             {'name': 'Ch561', 'gains': [1.0, 1.0, 1.25, 1.0]}]
    path = mosaic.save_gains(dataset, group, gains)
    assert path == dataset / 'gain.json'
    data = json.loads(path.read_text())
    assert data['acquisition'] == 'scan.ome.zarr'
    assert [entry['name'] for entry in data['channels']] == ['Ch488', 'Ch561']
    assert mosaic.load_gains(dataset, group) == {
        'acquisition': 'scan.ome.zarr', 'channels': gains,
    }
    view = mosaic.brightness_data(group, None, mosaic.load_gains(dataset, group))
    assert view['gains'] == [[1.0, 1.1, 0.9, 2.0], [1.0, 1.0, 1.25, 1.0]]

    for bad in (
        [{'name': 'Ch488', 'gains': [1.0, 1.1, 0.9]}, {'name': 'Ch561', 'gains': [1.0] * 4}],
        [{'name': 'Ch488', 'gains': [1.0, 0, 0.9, 2.0]}, {'name': 'Ch561', 'gains': [1.0] * 4}],
        [{'name': 'Ch488', 'gains': [1.0, -1, 0.9, 2.0]}, {'name': 'Ch561', 'gains': [1.0] * 4}],
        [{'name': 'Ch488', 'gains': [1.0, 1000, 0.9, 2.0]}, {'name': 'Ch561', 'gains': [1.0] * 4}],
        [{'name': 'Wrong', 'gains': [1.0] * 4}, {'name': 'Ch561', 'gains': [1.0] * 4}],
        [{'name': 'Ch488', 'gains': [1.0] * 4}],
        'nope',
    ):
        with pytest.raises(mosaic.MosaicError):
            mosaic.save_gains(dataset, group, bad)
    assert mosaic.load_gains(dataset, group)['channels'][0]['gains'] == [1.0, 1.1, 0.9, 2.0]


def test_gains_are_only_reused_for_matching_acquisition(dataset):
    shutil.copytree(dataset / 'scan.ome.zarr', dataset / 'second.ome.zarr')
    second, _ = mosaic.load_group(dataset, 1)
    first, _ = mosaic.load_group(dataset, 0)
    mosaic.save_gains(dataset, first, [{'name': 'Ch488', 'gains': [1.0] * 4},
                                       {'name': 'Ch561', 'gains': [1.0] * 4}])
    assert mosaic.load_gains(dataset, second) is None
    assert mosaic.load_gains(dataset, first) is not None


def test_rejects_invalid_saved_gain_file(dataset):
    group, _ = mosaic.load_group(dataset, 0)
    path = dataset / 'gain.json'
    for content in ('not json', '{"acquisition": "other.ome.zarr"}', '[]',
                    '{"acquisition": "scan.ome.zarr", "channels": [{"name": "Ch488"}]}',
                    '{"acquisition": "scan.ome.zarr", "channels": '
                    '[{"name": "Ch488", "gains": [1, 2, 3]}]}',
                    '{"acquisition": "scan.ome.zarr", "channels": '
                    '[{"name": "Ch488", "gains": [NaN, 2, 3, 4]}, '
                    '{"name": "Ch561", "gains": [1, 2, 3, 4]}]}'):
        path.write_text(content)
        assert mosaic.load_gains(dataset, group) is None


def test_render_patch_applies_gain_and_channel(dataset):
    group, _ = mosaic.load_group(dataset, 0)
    plain = Image.open(BytesIO(mosaic.render_patch(group, 0, 0, 0, 0, 2, 0, 200)))
    assert plain.getpixel((0, 0)) == 153  # 120 / 200 * 255
    bright = Image.open(BytesIO(mosaic.render_patch(group, 0, 0, 0, 0, 2, 0, 200, gain=2.0)))
    assert bright.getpixel((0, 0)) == 255  # 240 / 200 * 255 clipped
    dim = Image.open(BytesIO(mosaic.render_patch(group, 0, 0, 0, 0, 2, 0, 200, gain=0.5)))
    assert dim.getpixel((0, 0)) == 76  # 60 / 200 * 255 truncated
    other = Image.open(BytesIO(mosaic.render_patch(group, 0, 0, 0, 0, 2, 0, 200,
                                                   channel_index=1)))
    assert other.getpixel((0, 0)) == 216  # Ch561: 170 / 200 * 255

    for gain in (0, -1, float('nan'), float('inf'), 1000):
        with pytest.raises(mosaic.MosaicError, match='gain'):
            mosaic.render_patch(group, 0, 0, 0, 0, 2, 0, 200, gain=gain)
    with pytest.raises(mosaic.MosaicError, match='Channel'):
        mosaic.render_patch(group, 0, 0, 0, 0, 2, 0, 200, channel_index=5)


def test_brightness_routes_and_authenticated_save(dataset, monkeypatch):
    pytest.importorskip('flask_sqlalchemy')
    import app as app_module

    monkeypatch.setattr(app_module, 'mosaic_dataset', lambda dataset_id: SimpleNamespace(
        id=dataset_id, name='Test dataset', modality='mesospim',
        path_on_fast_store=str(dataset), path_on_hive=None, moved=False
    ))
    monkeypatch.setitem(app_module.app.config, 'TESTING', True)
    monkeypatch.setitem(app_module.app.config, 'SESSION_COOKIE_SECURE', False)
    client = app_module.app.test_client()
    assert client.get('/datasets/1/brightness').status_code == 302
    with client.session_transaction() as login_session:
        login_session['_user_id'] = 'CBI_Admin'
        login_session['_fresh'] = True

    response = client.get('/datasets/1/brightness')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    match = re.search(
        r'<script id="brightness-config" type="application/json">(.*?)</script>',
        html, re.S)
    config = json.loads(match.group(1))
    assert config['data']['rows'] == 2
    assert config['data']['arms'] == ['L', 'R', 'L', 'R']
    assert config['data']['gains'] == [[1.0] * 4, [1.0] * 4]
    assert 'z-plane' in html
    assert 'brightness.js' in html

    image = client.get('/datasets/1/brightness/tiles/0/0/0/0/0/0.png?z=2&lo=0&hi=200')
    assert image.status_code == 200
    assert Image.open(BytesIO(image.data)).size == (8, 8)
    assert Image.open(BytesIO(image.data)).getpixel((0, 0)) == 153
    bright = client.get(
        '/datasets/1/brightness/tiles/0/0/0/0/0/0.png?z=2&lo=0&hi=200&gain=2.0')
    assert Image.open(BytesIO(bright.data)).getpixel((0, 0)) == 255
    other = client.get('/datasets/1/brightness/tiles/0/1/0/0/0/0.png?z=2&lo=0&hi=200')
    assert Image.open(BytesIO(other.data)).getpixel((0, 0)) == 216
    assert client.get(
        '/datasets/1/brightness/tiles/0/0/0/0/0/0.png?z=10&lo=0&hi=200').status_code == 400
    assert client.get(
        '/datasets/1/brightness/tiles/0/0/0/0/0/0.png?z=2&lo=0&hi=200&gain=0').status_code == 400
    assert client.get(
        '/datasets/1/brightness/tiles/0/0/0/0/0/0.png?z=2&lo=0&hi=200&gain=1000'
    ).status_code == 400
    assert client.get(
        '/datasets/1/brightness/tiles/0/5/0/0/0/0.png?z=2&lo=0&hi=200').status_code == 400

    save_url = '/datasets/1/brightness/0'
    channels = [{'name': 'Ch488', 'gains': [1.0, 1.1, 0.9, 1.2]},
                {'name': 'Ch561', 'gains': [1.0] * 4}]
    rejected = client.post(save_url, json={'channels': channels})
    assert rejected.status_code == 403
    assert rejected.is_json
    assert 'authorization' in rejected.json['error'].lower()
    wrong_salt = app_module.mosaic_token_serializer().dumps(['CBI_Admin', 1, 0])
    rejected = client.post(save_url, json={'channels': channels},
                           headers={'X-CSRF-Token': wrong_salt})
    assert rejected.status_code == 403
    response = client.post(save_url, json={'channels': channels},
                           headers={'X-CSRF-Token': config['csrf_token']})
    assert response.status_code == 200
    assert (dataset / 'gain.json').exists()
    saved = json.loads((dataset / 'gain.json').read_text())
    assert saved['acquisition'] == 'scan.ome.zarr'
    assert saved['channels'][0]['gains'] == [1.0, 1.1, 0.9, 1.2]

    html = client.get('/datasets/1/brightness').get_data(as_text=True)
    config = json.loads(re.search(
        r'<script id="brightness-config" type="application/json">(.*?)</script>',
        html, re.S).group(1))
    assert config['data']['gains'] == [[1.0, 1.1, 0.9, 1.2], [1.0] * 4]
    assert config['data']['saved'] is True

    bad = [{'name': 'Ch488', 'gains': [1.0, 1.1]}, {'name': 'Ch561', 'gains': [1.0] * 4}]
    rejected = client.post(save_url, json={'channels': bad},
                           headers={'X-CSRF-Token': config['csrf_token']})
    assert rejected.status_code == 400
    assert rejected.is_json
