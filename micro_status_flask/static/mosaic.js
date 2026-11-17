(() => {
    'use strict';

    const config = JSON.parse(document.getElementById('mosaic-config').textContent);
    const data = config.data;
    const positions = data.positions_zyx.map(position => position.slice());
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

    if (!window.OpenSeadragon) {
        status.textContent = 'The zoomable image viewer could not be loaded.';
        return;
    }

    const viewer = OpenSeadragon({
        id: 'tile-viewer', showNavigationControl: false, imageLoaderLimit: 2,
        maxImageCacheCount: 120, immediateRender: true, blendTime: 0.1,
        visibilityRatio: 0.1
    });
    window.__mosaicDebugViewer = viewer;
    const items = [];
    const overlays = [];
    let dirty = false;
    let reloadTimer;
    let moveMode = false;
    let drag = null;

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

    function positionPoint(i) {
        return new OpenSeadragon.Point(positions[i][2] / baseWidth, positions[i][1] / baseWidth);
    }

    function centerPoint(i) {
        const tile = data.tiles[i].levels.at(-1);
        return new OpenSeadragon.Point(
            (positions[i][2] + tile.width / 2) / baseWidth,
            (positions[i][1] + tile.height / 2) / baseWidth
        );
    }

    function moveTile(i, dy, dx) {
        if (!dy && !dx) return;
        positions[i][1] += dy;
        positions[i][2] += dx;
        items[i].setPosition(positionPoint(i), true);
        viewer.updateOverlay(overlays[i], centerPoint(i), OpenSeadragon.Placement.CENTER);
        dirty = true;
        message('Unsaved tile positions.');
    }

    function makeOverlay(i) {
        const element = document.createElement('div');
        element.className = 'tile-arrows';
        element.innerHTML = `<button class="up" data-dy="-1" aria-label="Move tile ${i} up">↑</button>
            <button class="left" data-dx="-1" aria-label="Move tile ${i} left">←</button>
            <span class="number">${i}</span>
            <button class="right" data-dx="1" aria-label="Move tile ${i} right">→</button>
            <button class="down" data-dy="1" aria-label="Move tile ${i} down">↓</button>`;
        for (const eventName of ['pointerdown', 'pointerup', 'dblclick', 'wheel']) {
            element.addEventListener(eventName, event => event.stopPropagation());
        }
        element.addEventListener('click', event => {
            const button = event.target.closest('button');
            if (!button) return;
            event.stopPropagation();
            const amount = event.shiftKey ? 10 : 1;
            moveTile(i, Number(button.dataset.dy || 0) * amount,
                Number(button.dataset.dx || 0) * amount);
        });
        return element;
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
        source.getTileUrl = (level, x, y) => `${config.image_root}/${i}/${level}/${x}/${y}.png` +
            `?z=${zInput.value}&lo=${lowInput.value}&hi=${highInput.value}`;
        return source;
    }

    function drawImages(preserveView = false) {
        drag = null;
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
                    const overlay = makeOverlay(i);
                    overlays[i] = overlay;
                    viewer.addOverlay({ element: overlay, location: centerPoint(i),
                        placement: OpenSeadragon.Placement.CENTER, checkResize: false });
                    if (++added === data.tiles.length) {
                        if (viewportBounds) viewer.viewport.fitBounds(viewportBounds, true);
                        else viewer.viewport.goHome(true);
                        showControls();
                        message(dirty ? 'Unsaved tile positions.' : 'Tiles loaded.');
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

    function setMode(shouldMove) {
        moveMode = shouldMove;
        drag = null;
        for (const [id, active] of [['pan-mode', !moveMode], ['move-mode', moveMode]]) {
            const button = document.getElementById(id);
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', String(active));
        }
        document.getElementById('tile-viewer').classList.toggle('move-mode', moveMode);
        message(dirty ? 'Unsaved tile positions.' :
            moveMode ? 'Move mode: drag a tile to reposition it.' : 'Pan mode: drag to navigate.');
    }

    function tileAt(viewportPoint) {
        // In an overlap, select the topmost tile as drawn by OpenSeadragon.
        for (let index = viewer.world.getItemCount() - 1; index >= 0; index--) {
            const image = viewer.world.getItemAt(index);
            const bounds = image.getBounds(true);
            if (viewportPoint.x >= bounds.x && viewportPoint.x < bounds.x + bounds.width &&
                    viewportPoint.y >= bounds.y && viewportPoint.y < bounds.y + bounds.height) {
                return items.indexOf(image);
            }
        }
        return -1;
    }

    viewer.addHandler('canvas-press', event => {
        if (!moveMode || (event.pointerType && !['mouse', 'pen'].includes(event.pointerType))) {
            return;
        }
        const point = viewer.viewport.pointFromPixel(event.position, true);
        const index = tileAt(point);
        if (index >= 0) {
            drag = { index, startPointer: event.position, startPosition: positions[index].slice(),
                moved: false };
        }
    });
    viewer.addHandler('canvas-drag', event => {
        if (!drag) return;
        event.preventDefaultAction = true;
        const offset = viewer.viewport.deltaPointsFromPixels(new OpenSeadragon.Point(
            event.position.x - drag.startPointer.x,
            event.position.y - drag.startPointer.y
        ), true);
        const y = drag.startPosition[1] + Math.round(offset.y * baseWidth);
        const x = drag.startPosition[2] + Math.round(offset.x * baseWidth);
        const dy = y - positions[drag.index][1];
        const dx = x - positions[drag.index][2];
        if (dy || dx) {
            moveTile(drag.index, dy, dx);
            drag.moved = true;
        }
    });
    viewer.addHandler('canvas-drag-end', event => {
        if (drag) event.preventDefaultAction = true;
        drag = null;
    });
    viewer.addHandler('canvas-release', () => {
        if (drag && !drag.moved) drag = null;
    });
    document.getElementById('pan-mode').addEventListener('click', () => setMode(false));
    document.getElementById('move-mode').addEventListener('click', () => setMode(true));
    document.getElementById('zoom-in').addEventListener('click', () => {
        viewer.viewport.zoomBy(1.5);
        viewer.viewport.applyConstraints();
    });
    document.getElementById('zoom-out').addEventListener('click', () => {
        viewer.viewport.zoomBy(1 / 1.5);
        viewer.viewport.applyConstraints();
    });
    document.getElementById('save-positions').addEventListener('click', async event => {
        event.target.disabled = true;
        message('Saving positions…');
        try {
            const response = await fetch(config.save_url, {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': config.csrf_token },
                body: JSON.stringify({ positions_zyx: positions })
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
    const selector = document.getElementById('acquisition');
    if (selector) selector.addEventListener('change', () => {
        if (dirty && !window.confirm('Discard unsaved tile positions?')) {
            selector.value = new URLSearchParams(window.location.search).get('acquisition') || '0';
            return;
        }
        window.location.href = `${config.viewer_url}?acquisition=${selector.value}`;
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
