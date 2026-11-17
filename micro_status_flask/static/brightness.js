(() => {
    'use strict';

    const config = JSON.parse(document.getElementById('brightness-config').textContent);
    const data = config.data;
    const positions = data.positions_zyx.map(position => position.slice());
    const gains = data.gains.map(channel => channel.map(gain => Number(gain)));
    const gainLimits = data.gain_limits;
    const baseWidth = data.tiles[0].levels.at(-1).width;
    const status = document.getElementById('viewer-status');
    const zInput = document.getElementById('z-plane');
    const lowInput = document.getElementById('contrast-low');
    const highInput = document.getElementById('contrast-high');
    const bounds = data.contrast;
    const step = Number.isInteger(bounds.max) && Number.isInteger(bounds.min)
        ? 1 : Math.max((bounds.max - bounds.min) / 1000, 0.0001);
    for (const input of [lowInput, highInput]) {
        input.min = bounds.min;
        input.max = bounds.max;
        input.step = step;
    }
    lowInput.value = bounds.low;
    highInput.value = bounds.high;

    let channelIndex = 0;
    let dirty = false;
    let reloadTimer;

    if (!window.OpenSeadragon) {
        status.textContent = 'The zoomable image viewer could not be loaded.';
        return;
    }

    const viewer = OpenSeadragon({
        id: 'tile-viewer', showNavigationControl: false, imageLoaderLimit: 2,
        maxImageCacheCount: 120, immediateRender: true, blendTime: 0.1,
        visibilityRatio: 0.1
    });
    const items = [];
    const overlays = [];

    function message(text, error = false) {
        status.textContent = text;
        status.className = error ? 'mb-2 text-danger' : 'mb-2 text-muted';
    }

    function showControls() {
        if (!viewer.viewport) return;
        const width = viewer.viewport.deltaPixelsFromPoints(
            new OpenSeadragon.Point(data.tiles[0].levels.at(-1).width / baseWidth, 0)
        ).x;
        overlays.forEach(overlay => { overlay.classList.toggle('is-hidden', width < 90); });
    }

    function centerPoint(i) {
        const tile = data.tiles[i].levels.at(-1);
        return new OpenSeadragon.Point(
            (positions[i][2] + tile.width / 2) / baseWidth,
            (positions[i][1] + tile.height / 2) / baseWidth
        );
    }

    function armClass(arm) {
        return arm === 'L' ? 'left' : arm === 'R' ? 'right' : 'unknown';
    }

    function armTitle(arm) {
        return arm === 'L' ? 'Acquired with the left arm'
            : arm === 'R' ? 'Acquired with the right arm' : 'Acquisition arm unknown';
    }

    function makeOverlay(i) {
        const arm = data.arms[i];
        const element = document.createElement('div');
        element.className = 'tile-gain';
        element.innerHTML = `<span class="number">${i}</span>
            <span class="arm ${armClass(arm)}" title="${armTitle(arm)}">${arm}</span>
            <button class="dimmer" aria-label="Darken tile ${i}">−</button>
            <span class="multiplier">×${gains[channelIndex][i].toFixed(2)}</span>
            <button class="brighter" aria-label="Brighten tile ${i}">+</button>`;
        for (const eventName of ['pointerdown', 'pointerup', 'dblclick', 'wheel']) {
            element.addEventListener(eventName, event => event.stopPropagation());
        }
        element.addEventListener('click', event => {
            const button = event.target.closest('button');
            if (!button) return;
            event.stopPropagation();
            const factor = event.shiftKey ? Math.pow(1.1, 5) : 1.1;
            setGain(i, button.classList.contains('brighter') ? factor : 1 / factor);
        });
        return element;
    }

    function setGain(i, factor) {
        const current = gains[channelIndex][i];
        const value = Math.min(gainLimits[1], Math.max(gainLimits[0],
            Math.round(current * factor * 100) / 100));
        if (value === current) return;
        gains[channelIndex][i] = value;
        dirty = true;
        overlays[i].querySelector('.multiplier').textContent = `×${value.toFixed(2)}`;
        message('Unsaved brightness adjustments.');
        queueReload();
    }

    function sourceFor(i) {
        const levels = data.tiles[i].levels;
        const source = new OpenSeadragon.TileSource({
            width: levels.at(-1).width, height: levels.at(-1).height,
            tileSize: data.tile_size, minLevel: 0, maxLevel: levels.length - 1
        });
        source.getLevelScale = level => levels[level].width / levels.at(-1).width;
        source.getNumTiles = level => new OpenSeadragon.Point(
            Math.ceil(levels[level].width / data.tile_size),
            Math.ceil(levels[level].height / data.tile_size)
        );
        source.getTileUrl = (level, x, y) => `${config.image_root}/${channelIndex}/${i}/${level}/${x}/${y}.png` +
            `?z=${zInput.value}&lo=${lowInput.value}&hi=${highInput.value}` +
            `&gain=${gains[channelIndex][i]}`;
        return source;
    }

    function drawImages(preserveView = false) {
        const viewportBounds = preserveView && viewer.viewport ? viewer.viewport.getBounds(true) : null;
        viewer.clearOverlays();
        viewer.world.removeAll();
        items.length = 0;
        overlays.length = 0;
        let added = 0;
        data.tiles.forEach((tile, i) => {
            viewer.addTiledImage({
                tileSource: sourceFor(i), x: positions[i][2] / baseWidth,
                y: positions[i][1] / baseWidth,
                width: tile.levels.at(-1).width / baseWidth,
                opacity: 0.8,
                success: event => {
                    items[i] = event.item;
                    overlays[i] = makeOverlay(i);
                    viewer.addOverlay({ element: overlays[i], location: centerPoint(i),
                        placement: OpenSeadragon.Placement.CENTER, checkResize: false });
                    if (++added === data.tiles.length) {
                        if (viewportBounds) viewer.viewport.fitBounds(viewportBounds, true);
                        else viewer.viewport.goHome(true);
                        showControls();
                        message(dirty ? 'Unsaved brightness adjustments.' :
                            data.saved ? 'Showing saved brightness adjustments.' : 'Tiles loaded.');
                    }
                },
                error: () => message(`Could not load tile ${i}.`, true)
            });
        });
    }

    function updateLabels() {
        document.getElementById('z-value').textContent = `${zInput.value} / ${data.z_count - 1}`;
        document.getElementById('contrast-value').textContent =
            `${Number(lowInput.value).toFixed(1)} – ${Number(highInput.value).toFixed(1)}`;
    }

    function queueReload() {
        updateLabels();
        clearTimeout(reloadTimer);
        reloadTimer = setTimeout(() => drawImages(true), 300);
    }

    zInput.addEventListener('input', queueReload);
    lowInput.addEventListener('input', () => {
        if (Number(lowInput.value) >= Number(highInput.value)) {
            lowInput.value = Number(highInput.value) - step;
        }
        queueReload();
    });
    highInput.addEventListener('input', () => {
        if (Number(highInput.value) <= Number(lowInput.value)) {
            highInput.value = Number(lowInput.value) + step;
        }
        queueReload();
    });
    viewer.addHandler('viewport-change', showControls);
    viewer.addHandler('tile-load-failed', () => message('An image patch could not be loaded.', true));

    document.getElementById('zoom-in').addEventListener('click', () => {
        viewer.viewport.zoomBy(1.5);
        viewer.viewport.applyConstraints();
    });
    document.getElementById('zoom-out').addEventListener('click', () => {
        viewer.viewport.zoomBy(1 / 1.5);
        viewer.viewport.applyConstraints();
    });
    document.getElementById('save-gains').addEventListener('click', async event => {
        event.target.disabled = true;
        message('Saving adjustments…');
        try {
            const response = await fetch(config.save_url, {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': config.csrf_token },
                body: JSON.stringify({ channels: data.channels.map((name, index) =>
                    ({ name: name, gains: gains[index] })) })
            });
            if (response.redirected) {
                throw new Error('Sign-in expired. Sign in again before saving.');
            }
            const contentType = response.headers.get('Content-Type') || '';
            const result = contentType.includes('application/json')
                ? await response.json() : null;
            if (!response.ok || !result) {
                throw new Error(result?.error || `Save failed (${response.status}).`);
            }
            dirty = false;
            message(result.message);
        } catch (error) {
            message(error.message, true);
        } finally {
            event.target.disabled = false;
        }
    });
    const acquisitionSelector = document.getElementById('acquisition');
    if (acquisitionSelector) acquisitionSelector.addEventListener('change', () => {
        if (dirty && !window.confirm('Discard unsaved brightness adjustments?')) {
            acquisitionSelector.value =
                new URLSearchParams(window.location.search).get('acquisition') || '0';
            return;
        }
        window.location.href = `${config.viewer_url}?acquisition=${acquisitionSelector.value}`;
    });
    document.getElementById('channel').addEventListener('change', event => {
        channelIndex = Number(event.target.value);
        message(`Displaying ${data.channels[channelIndex]}.`);
        drawImages(true);
    });
    window.addEventListener('beforeunload', event => {
        if (dirty) {
            event.preventDefault();
            event.returnValue = '';
        }
    });
    updateLabels();
    drawImages();
})();
