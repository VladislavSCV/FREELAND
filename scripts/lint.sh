#!/bin/sh
set -eu
pip install -q ruff
ruff check "$@" /code/app /code/tests
