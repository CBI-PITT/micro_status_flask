import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'micro_status_flask'))


def test_flask_and_neuroglancer_share_one_origin():
    import app as app_module
    from server import create_asgi_app

    combined = create_asgi_app(app_module.app)

    async def check():
        transport = httpx.ASGITransport(app=combined)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            assert (await client.get('/login')).status_code == 200
            # The service's catch-all dataset route must not shadow Flask.
            edit = await client.get('/datasets/1/edit')
            assert edit.status_code == 302
            assert '/login' in edit.headers['location']
            assert (await client.get('/virtual-zarr/healthz')).json()['status'] == 'ok'
            assert (await client.get('/virtual-zarr/datasets/1/viewer')).status_code == 403
            viewer = await client.get('/virtual-zarr/v/viewer/index.html')
            assert viewer.status_code == 200
            assert 'text/html' in viewer.headers['content-type']

    asyncio.run(check())
