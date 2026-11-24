"""One HTTP server for the Flask dashboard and virtual MesoSPIM viewer."""

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI
import uvicorn

import settings
from virtual_mesospim.service import create_service_app, default_client_dir


def create_asgi_app(flask_app):
    client_dir = default_client_dir()
    if client_dir is None:
        raise RuntimeError('Neuroglancer client not found; install neuroglancer.')

    combined = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    combined.mount(settings.VIRTUAL_ZARR_SERVICE_PATH,
                   create_service_app(flask_app.secret_key, client_dir=client_dir))
    combined.mount('/', WSGIMiddleware(flask_app))
    return combined


def main(flask_app):
    uvicorn.run(create_asgi_app(flask_app), host='0.0.0.0', port=1414, workers=1)
