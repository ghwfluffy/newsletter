#!/bin/bash

set -eux -o pipefail

python3 -m venv ./venv
source ./venv/bin/activate
pip3 install -r requirements.txt

SOURCES=($(find src -name '*.py'))

mypy "${SOURCES[@]}"
ruff check --line-length 100 --fix "${SOURCES[@]}"
flake8 "${SOURCES[@]}"
