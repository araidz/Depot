#!/bin/sh
# Build the single-file `slipway` executable with stdlib zipapp.
set -e
cd "$(dirname "$0")"
rm -rf build dist
mkdir -p build dist
cp -r slipway build/slipway
rm -rf build/slipway/__pycache__
printf 'import sys\nfrom slipway.__main__ import main\nsys.exit(main())\n' > build/__main__.py
python3 -m zipapp build -o dist/slipway -p "/usr/bin/env python3"
chmod +x dist/slipway
rm -rf build
echo "built dist/slipway  —  install: ln -sf \"$(pwd)/dist/slipway\" /usr/local/bin/slipway"
