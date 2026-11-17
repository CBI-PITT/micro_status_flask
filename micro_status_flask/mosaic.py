"""Read-only MesoSPIM tile discovery and on-demand 2-D image rendering.

The only disk writes in this module are explicitly requested mosaic.json and
gain.json saves. All paths used for image requests come from the dataset
directory, never a URL.
"""

import json
import math
import os
import re
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from statistics import median
from threading import BoundedSemaphore

TILE_SIZE = 512
TILE_NAME = re.compile(r"(?:^|_)Tile(\d+)(?:_|\.)", re.IGNORECASE)
CHANNEL_NAME = re.compile(r"(?:^|_)Ch([A-Za-z0-9]+)(?:_|\.)", re.IGNORECASE)
IMAGE_READS = BoundedSemaphore(1)
GAIN_MIN = 0.01
GAIN_MAX = 100.0


class MosaicError(ValueError):
    """A dataset cannot be used as a MesoSPIM tile mosaic."""


def dataset_directory(dataset):
    paths = (dataset.path_on_hive, dataset.path_on_fast_store) if dataset.moved else (
        dataset.path_on_fast_store, dataset.path_on_hive
    )
    # Some older records retain only the original FastStore path after a move.
    import settings

    fast_path = dataset.path_on_fast_store or ''
    if fast_path.startswith(settings.FASTSTORE_ACQUISITION_FOLDER):
        inferred_hive = fast_path.replace(settings.FASTSTORE_ACQUISITION_FOLDER,
                                          settings.HIVE_ACQUISITION_FOLDER, 1)
        paths += (inferred_hive,)
    for path in paths:
        if path and Path(path).is_absolute() and Path(path).is_dir():
            return Path(path).resolve()
    raise MosaicError("Neither the FastStore nor the Hive dataset folder is accessible.")


def _tile_paths(folder):
    return [path for path in folder.iterdir() if path.is_dir()
            and path.name.endswith('.ome.zarr') and TILE_NAME.search(path.name)
            and (path / '.zattrs').is_file() and (path / '.zgroup').is_file()]


def list_acquisitions(root):
    """Look in the dataset directory and one nested acquisition directory."""
    root = Path(root)
    candidates = [root] if root.name.endswith('.ome.zarr') else []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name.endswith('.ome.zarr'):
            candidates.append(child)
        elif child.name not in ('decon', 'bigstitcher', 'logs_mesospim_utils'):
            candidates.extend(path for path in child.iterdir()
                              if path.is_dir() and path.name.endswith('.ome.zarr'))
    return sorted((path for path in candidates if _tile_paths(path)),
                  key=lambda path: str(path.relative_to(root)))


def _round(value):
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def _metadata(path):
    import numpy as np

    try:
        attrs = json.loads((path / '.zattrs').read_text(encoding='utf-8'))
        multiscales = attrs['multiscales']
        if len(multiscales) != 1 or multiscales[0].get('version') != '0.4':
            raise MosaicError(f'{path.name}: expected one OME-Zarr 0.4 multiscale image')
        ms = multiscales[0]
        axes = [axis['name'] if isinstance(axis, dict) else axis for axis in ms['axes']]
        if axes[-3:] != ['z', 'y', 'x'] or axes[:-3] not in (
            [], ['c'], ['t'], ['t', 'c']
        ):
            raise MosaicError(f'{path.name}: unsupported axes {axes}')
        levels = []
        dtype = None
        for level in ms['datasets']:
            name = level['path']
            if not re.fullmatch(r'\d+', name):
                raise MosaicError(f'{path.name}: invalid pyramid level path')
            array = json.loads((path / name / '.zarray').read_text(encoding='utf-8'))
            if array.get('zarr_format') != 2:
                raise MosaicError(f'{path.name}: expected Zarr v2 tile arrays')
            level_dtype = np.dtype(array['dtype'])
            if dtype is not None and dtype != level_dtype:
                raise MosaicError(f'{path.name}: pyramid levels have different pixel types')
            dtype = level_dtype
            transforms = level['coordinateTransformations']
            scale = next(item['scale'] for item in transforms if item['type'] == 'scale')
            translation = next((item['translation'] for item in transforms
                                if item['type'] == 'translation'), None)
            if translation is None or len(scale) != len(axes) or len(translation) != len(axes):
                raise MosaicError(f'{path.name}: missing stage translation or voxel scale')
            if any(not math.isfinite(float(v)) or float(v) <= 0 for v in scale):
                raise MosaicError(f'{path.name}: invalid voxel scale')
            if any(not math.isfinite(float(v)) for v in translation):
                raise MosaicError(f'{path.name}: invalid stage translation')
            levels.append({'path': name, 'shape': array['shape'],
                           'scale': [float(v) for v in scale[-3:]],
                           'translation': [float(v) for v in translation[-3:]]})
        if not levels or any(len(level['shape']) != len(axes) or
                             any(type(size) is not int or size < 1 for size in level['shape'])
                             for level in levels):
            raise MosaicError(f'{path.name}: invalid pyramid shape')
        if dtype.kind not in 'iuf' or dtype.itemsize > 4:
            raise MosaicError(f'{path.name}: unsupported pixel type {dtype}')
        return {'path': path, 'axes': axes, 'levels': levels, 'dtype': dtype.name}
    except (OSError, KeyError, IndexError, TypeError, StopIteration,
            json.JSONDecodeError) as exc:
        raise MosaicError(f'{path.name}: invalid OME-Zarr metadata: {exc}') from exc


@lru_cache(maxsize=1024)
def _shutter(meta_path):
    """Read the [Shutter] line from a tile's _meta.txt sidecar."""
    try:
        text = Path(meta_path).read_text(encoding='utf-8')
    except OSError:
        return None
    match = re.search(r'^\[Shutter\]\s*(.+?)\s*$', text, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).lower()
    if value.startswith('left'):
        return 'L'
    if value.startswith('right'):
        return 'R'
    return '?'


def _tile_arm(tile):
    """Arm used to acquire a tile, from its _meta.txt sidecar next to the
    acquisition folder, named {acquisition}_{tile}_meta.txt."""
    acquisition = tile['path'].parent
    meta = acquisition.parent / f"{acquisition.name}_{tile['path'].name}_meta.txt"
    return _shutter(meta) or '?'


def _clusters(values, tolerance):
    groups = []
    for value in sorted(values):
        if not groups or value - groups[-1][-1] > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _closest(value, centers):
    return min(range(len(centers)), key=lambda i: abs(value - centers[i]))


def _layout(tiles):
    first = tiles[0]['levels'][0]
    scale = first['scale']
    x_centers = _clusters((tile['levels'][0]['translation'][2] for tile in tiles),
                          scale[2] * 5)
    y_centers = _clusters((tile['levels'][0]['translation'][1] for tile in tiles),
                          scale[1] * 5)
    rows, cols = len(y_centers), len(x_centers)
    if rows * cols != len(tiles):
        raise MosaicError('Stage positions do not form a rectangular tile grid.')
    if rows > 1 and tiles[1]['levels'][0]['translation'][1] < first['translation'][1]:
        y_centers.reverse()
    if cols > 1 and tiles[rows]['levels'][0]['translation'][2] < first['translation'][2]:
        x_centers.reverse()
    cells = set()
    for i, tile in enumerate(tiles):
        translation = tile['levels'][0]['translation']
        cell = (_closest(translation[1], y_centers), _closest(translation[2], x_centers))
        if cell in cells or cell != (i % rows, i // rows):
            raise MosaicError('Tiles must be numbered in column-major stage order.')
        cells.add(cell)
    ref = first['translation']
    positions = [[_round((tile['levels'][0]['translation'][j] - ref[j]) / scale[j])
                  for j in range(3)] for tile in tiles]
    # Canonicalize orientation: tile 0 top-left, rows down, columns right,
    # following the column-major tile numbering (as validate_tiles.py does),
    # regardless of the stage sweep direction along y and x.
    if rows > 1 and tiles[1]['levels'][0]['translation'][1] < first['translation'][1]:
        for position in positions:
            position[1] = -position[1]
    if cols > 1 and tiles[rows]['levels'][0]['translation'][2] < first['translation'][2]:
        for position in positions:
            position[2] = -position[2]
    return rows, cols, positions


def _manifest_tiles(root, group):
    channels = group['channels']
    if len(channels) == 1:
        return {'tiles': [os.path.relpath(tile['path'], root)
                          for tile in channels[0]['tiles']]}
    return {'channels': [
        {'name': channel['name'], 'tiles': [os.path.relpath(tile['path'], root)
                                           for tile in channel['tiles']]}
        for channel in channels
    ]}


def _matching_manifest(root, group):
    path = root / 'mosaic.json'
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return None
        if data.get('rows') != group['rows'] or data.get('cols') != group['cols']:
            return None
        expected = _manifest_tiles(root, group)
        if 'tiles' in expected:
            actual = [data['tiles']]
            wanted = [expected['tiles']]
        else:
            channels = data['channels']
            if [channel['name'] for channel in channels] != [
                channel['name'] for channel in expected['channels']
            ]:
                return None
            actual = [channel['tiles'] for channel in channels]
            wanted = [channel['tiles'] for channel in expected['channels']]
        for paths, tiles in zip(actual, wanted):
            if len(paths) != len(tiles) or any(
                (root / path).resolve() != (root / tile).resolve()
                for path, tile in zip(paths, tiles)
            ):
                return None
        if len(actual) != len(wanted):
            return None
        positions = data['positions_zyx']
        if not _valid_positions(positions, len(group['channels'][0]['tiles'])):
            return None
        return data
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _valid_positions(positions, count):
    return (isinstance(positions, list) and len(positions) == count
            and all(isinstance(p, list) and len(p) == 3
                    and all(type(v) is int and abs(v) <= 100_000_000 for v in p)
                    for p in positions))


def _group(root, acquisition):
    paths = list_acquisitions(root)
    if not 0 <= acquisition < len(paths):
        raise MosaicError('Acquisition not found in the dataset folder.')
    folder = paths[acquisition]
    by_channel = {}
    for path in _tile_paths(folder):
        match = TILE_NAME.search(path.name)
        channel = CHANNEL_NAME.search(path.name)
        name = f'Ch{channel.group(1)}' if channel else 'default'
        number = int(match.group(1))
        if number in by_channel.setdefault(name, {}):
            raise MosaicError(f'Duplicate tile {number} in {name}.')
        by_channel[name][number] = _metadata(path)
    channels = []
    for name, numbered in sorted(by_channel.items()):
        if sorted(numbered) != list(range(len(numbered))):
            raise MosaicError(f'{name}: tile numbers must start at zero with no gaps.')
        channels.append({'name': name, 'tiles': [numbered[i] for i in sorted(numbered)]})
    if not channels:
        raise MosaicError('No MesoSPIM OME-Zarr tiles found.')
    tiles = channels[0]['tiles']
    rows, cols, stage_positions = _layout(tiles)
    # Tiles may differ in z-plane count (acquisitions stopped and resumed);
    # y/x sizes, axes, pixel type and level-0 voxel scale must still match.
    signature = [(tile['levels'][0]['shape'][-2:], tile['axes'], tile['dtype'],
                  tile['levels'][0]['scale']) for tile in tiles]
    if any(entry != signature[0] for entry in signature):
        raise MosaicError('Tile sizes (z depth may differ), axes, types and '
                          'level-0 voxel sizes must match.')
    for channel in channels[1:]:
        if len(channel['tiles']) != len(tiles) or any(
            (other['levels'][0]['shape'][-2:], other['axes'], other['dtype'],
             other['levels'][0]['scale']) != signature[0]
            for other in channel['tiles']
        ):
            raise MosaicError('All channels must have matching tiles and voxel geometry.')
        other_rows, other_cols, other_positions = _layout(channel['tiles'])
        if (other_rows, other_cols) != (rows, cols) or any(
            abs(other_positions[i][axis] - stage_positions[i][axis]) > 5
            for i in range(len(tiles)) for axis in (1, 2)
        ):
            raise MosaicError('Channel tile numbers must share the same stage grid.')
    result = {'folder': folder, 'name': str(folder.relative_to(root)), 'channels': channels,
              'rows': rows, 'cols': cols, 'stage_positions': stage_positions}
    return result


@lru_cache(maxsize=32)
def _cached_group(root_string, acquisition, minute):
    return _group(Path(root_string), acquisition)


def load_group(root, acquisition):
    root = Path(root)
    group = _cached_group(str(root), acquisition, int(time.time() // 60))
    manifest = _matching_manifest(root, group)
    return group, manifest


def _contrast(tiles):
    """Estimate a useful common starting window from coarse, nonzero pixels."""
    import numpy as np
    import zarr

    dtype = np.dtype(tiles[0]['dtype'])
    samples = []
    for tile in tiles[:4]:
        level = tile['levels'][-1]
        array = zarr.open_array(str(tile['path'] / level['path']), mode='r')
        z = level['shape'][-3] // 2
        plane = np.asarray(array[(0,) * (len(tile['axes']) - 3) + (z, slice(None), slice(None))])
        sample = plane[::4, ::4].ravel()
        samples.append(sample[np.isfinite(sample) & (sample != 0)])
    nonzero = np.concatenate(samples) if samples else np.array([])
    if dtype.kind in 'iu':
        limits = np.iinfo(dtype)
        minimum, maximum = int(limits.min), int(limits.max)
    elif nonzero.size:
        minimum, maximum = float(np.min(nonzero)), float(np.max(nonzero))
    else:
        minimum, maximum = 0, 1
    if maximum <= minimum:
        maximum = minimum + 1
    if nonzero.size:
        low = max(minimum, float(np.percentile(nonzero, 1)) * 0.5)
        high = min(maximum, float(np.percentile(nonzero, 99.8)))
    else:
        low, high = minimum, maximum
    if high <= low:
        low, high = minimum, maximum
    return {'min': minimum, 'max': maximum, 'low': low, 'high': high}


def view_data(root, group, manifest):
    tiles = group['channels'][0]['tiles']
    positions = manifest['positions_zyx'] if manifest else group['stage_positions']
    return {
        'name': group['name'], 'rows': group['rows'], 'cols': group['cols'],
        'channels': [channel['name'] for channel in group['channels']],
        'saved': manifest is not None, 'positions_zyx': positions,
        'z_count': max(tile['levels'][0]['shape'][-3] for tile in tiles),
        'tile_size': TILE_SIZE,
        'contrast': _contrast(tiles),
        'tiles': [{'number': i, 'levels': [
            {'width': level['shape'][-1], 'height': level['shape'][-2]}
            for level in reversed(tile['levels'])
        ]} for i, tile in enumerate(tiles)],
    }


def brightness_data(group, manifest, saved):
    """Assemble viewer context for the lighting adjustment page."""
    tiles = group['channels'][0]['tiles']
    positions = manifest['positions_zyx'] if manifest else group['stage_positions']
    defaults = [1.0] * len(tiles)
    saved_by_name = {channel['name']: channel['gains']
                     for channel in (saved or {}).get('channels', [])}
    return {
        'name': group['name'], 'rows': group['rows'], 'cols': group['cols'],
        'channels': [channel['name'] for channel in group['channels']],
        'gains': [saved_by_name.get(channel['name'], defaults)
                  for channel in group['channels']],
        'arms': [_tile_arm(tile) for tile in tiles],
        'gain_limits': [GAIN_MIN, GAIN_MAX],
        'saved': saved is not None, 'positions_zyx': positions,
        'z_count': max(tile['levels'][0]['shape'][-3] for channel in group['channels']
                       for tile in channel['tiles']),
        'tile_size': TILE_SIZE,
        'contrast': _contrast(tiles),
        'tiles': [{'number': i, 'levels': [
            {'width': level['shape'][-1], 'height': level['shape'][-2]}
            for level in reversed(tile['levels'])
        ]} for i, tile in enumerate(tiles)],
    }


def _valid_gains(gains, count):
    return (isinstance(gains, list) and len(gains) == count
            and all(type(v) in (int, float) and math.isfinite(v)
                    and GAIN_MIN <= v <= GAIN_MAX for v in gains))


def load_gains(root, group):
    """Return the saved brightness gains for this acquisition, or None."""
    path = root / 'gain.json'
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('acquisition') != group['name']:
            return None
        channels = data.get('channels')
        if not isinstance(channels, list) or len(channels) != len(group['channels']):
            return None
        for channel, expected in zip(channels, group['channels']):
            if (not isinstance(channel, dict)
                    or channel.get('name') != expected['name']
                    or not _valid_gains(channel.get('gains'), len(expected['tiles']))):
                return None
        return {'acquisition': group['name'], 'channels': [
            {'name': channel['name'], 'gains': [float(v) for v in channel['gains']]}
            for channel in channels
        ]}
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def save_gains(root, group, channels):
    """Save per-tile brightness gains for later gain correction."""
    if not isinstance(channels, list) or len(channels) != len(group['channels']):
        raise MosaicError('Expected one gain list per channel.')
    data = {'acquisition': group['name'], 'channels': []}
    for channel, expected in zip(channels, group['channels']):
        if (not isinstance(channel, dict)
                or channel.get('name') != expected['name']
                or not _valid_gains(channel.get('gains'), len(expected['tiles']))):
            raise MosaicError(f'Expected one positive gain per tile for {expected["name"]}.')
        data['channels'].append({'name': expected['name'],
                                 'gains': [float(v) for v in channel['gains']]})
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root,
                                         prefix='.gain-', suffix='.json',
                                         delete=False) as output:
            temp = output.name
            json.dump(data, output, indent=2, allow_nan=False)
            output.write('\n')
        os.replace(temp, root / 'gain.json')
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)
    return root / 'gain.json'


@lru_cache(maxsize=4)
def _image_plane(path, level, z):
    import numpy as np
    import zarr

    array = zarr.open_array(str(Path(path) / level), mode='r')
    ndim = array.ndim
    return np.asarray(array[(0,) * (ndim - 3) + (z, slice(None), slice(None))])


def render_patch(group, tile_index, level_index, col, row, base_z, low, high,
                 channel_index=0, gain=1.0):
    import numpy as np
    from PIL import Image

    channels = group['channels']
    if not 0 <= channel_index < len(channels):
        raise MosaicError('Channel index out of range.')
    tiles = channels[channel_index]['tiles']
    if not 0 <= tile_index < len(tiles):
        raise MosaicError('Tile index out of range.')
    tile = tiles[tile_index]
    levels = tile['levels']
    if not 0 <= level_index < len(levels) or col < 0 or row < 0:
        raise MosaicError('Pyramid level or image patch out of range.')
    level = levels[-1 - level_index]
    width, height = level['shape'][-1], level['shape'][-2]
    x, y = col * TILE_SIZE, row * TILE_SIZE
    reference = group['channels'][0]['tiles'][0]['levels'][0]
    z_count = max(tile['levels'][0]['shape'][-3] for tile in group['channels'][0]['tiles'])
    if not 0 <= base_z < z_count or x >= width or y >= height:
        raise MosaicError('Z-plane or image patch out of range.')
    if not all(math.isfinite(v) for v in (low, high)) or high <= low:
        raise MosaicError('Invalid contrast limits.')
    if not math.isfinite(gain) or not GAIN_MIN <= gain <= GAIN_MAX:
        raise MosaicError('Invalid brightness gain.')
    dtype = np.dtype(tile['dtype'])
    limits = np.iinfo(dtype) if dtype.kind in 'iu' else None
    if limits and (low < limits.min or high > limits.max):
        raise MosaicError('Contrast limits out of pixel range.')
    physical_z = (reference['translation'][0] + base_z * reference['scale'][0])
    z = _round((physical_z - level['translation'][0]) / level['scale'][0])
    if z < 0 or z >= level['shape'][-3]:
        pixels = np.zeros((min(TILE_SIZE, height - y), min(TILE_SIZE, width - x)), dtype='uint8')
    else:
        # Zarr can decompress a much larger source chunk for a small patch.
        # Bound simultaneous reads of the high-resolution MesoSPIM chunks.
        with IMAGE_READS:
            pixels = _image_plane(str(tile['path']), level['path'], z)[
                y:y + TILE_SIZE, x:x + TILE_SIZE
            ]
        pixels = np.nan_to_num(pixels, nan=low, posinf=high, neginf=low)
        pixels = (np.clip((pixels.astype('float32') * gain - low) * (255 / (high - low)), 0, 255)
                  .astype('uint8'))
    from io import BytesIO

    output = BytesIO()
    Image.fromarray(pixels, mode='L').save(output, format='PNG')
    return output.getvalue()


def save_positions(root, group, positions):
    count = len(group['channels'][0]['tiles'])
    if not _valid_positions(positions, count):
        raise MosaicError('Expected one integer [z, y, x] position per tile.')
    existing = _matching_manifest(root, group)
    baseline = existing['positions_zyx'] if existing else group['stage_positions']
    if any(position[0] != baseline[i][0] for i, position in enumerate(positions)):
        raise MosaicError('Tile z positions cannot be changed in the 2-D viewer.')
    tiles = group['channels'][0]['tiles']
    rows, cols = group['rows'], group['cols']
    sy, sx = tiles[0]['levels'][0]['shape'][-2:]
    dy = median(abs(positions[i + 1][1] - positions[i][1])
                for i in range(count) if i % rows < rows - 1) if rows > 1 else sy * .9
    dx = median(abs(positions[i + rows][2] - positions[i][2])
                for i in range(count - rows)) if cols > 1 else sx * .9
    data = {
        **_manifest_tiles(root, group), 'rows': rows, 'cols': cols,
        'overlap_yx': [max(0, min(.99, 1 - dy / sy)),
                       max(0, min(.99, 1 - dx / sx))],
        'chunk_shape_zyx': [32, 256, 256], 'blend': 'feather',
        'positions_zyx': positions,
        'row_direction': 1 if rows == 1 or positions[1][1] >= positions[0][1] else -1,
        'col_direction': 1 if cols == 1 or positions[rows][2] >= positions[0][2] else -1,
        'gzip_level': 0, 'cache_chunks': 8, 'max_concurrent_chunks': 4,
        'name': group['folder'].stem, 'mount_path': 'stitched.ome.zarr',
    }
    if existing:
        for key in ('overlap_yx', 'chunk_shape_zyx', 'blend', 'gzip_level',
                    'cache_chunks', 'max_concurrent_chunks', 'name', 'mount_path'):
            if key in existing:
                data[key] = existing[key]
        if 'channels' in data:
            for channel, old in zip(data['channels'], existing['channels']):
                if 'color' in old:
                    channel['color'] = old['color']
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root,
                                         prefix='.mosaic-', suffix='.json',
                                         delete=False) as output:
            temp = output.name
            json.dump(data, output, indent=2, allow_nan=False)
            output.write('\n')
        os.replace(temp, root / 'mosaic.json')
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)
    return root / 'mosaic.json'
