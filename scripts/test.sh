#!/bin/sh
set -eu
pip install -q pytest ruff
ruff check /code/app /code/tests
pytest -q /code/tests
