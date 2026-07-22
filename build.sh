#!/bin/sh
# Build the single-file `depot` executable with stdlib zipapp.
set -e
cd "$(dirname "$0")"
rm -rf build dist
mkdir -p build dist
cp -r depot build/depot
rm -rf build/depot/__pycache__
printf 'import sys\nfrom depot.__main__ import main\nsys.exit(main())\n' > build/__main__.py
python3 -m zipapp build -o dist/depot -p "/usr/bin/env python3"
chmod +x dist/depot
rm -rf build
echo "built dist/depot  —  install: ln -sf \"$(pwd)/dist/depot\" /usr/local/bin/depot"
