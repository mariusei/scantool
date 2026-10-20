#!/bin/sh
# Build the release tarball for one version into a directory.
set -e
tar -czf "$2/release-$1.tar.gz" dist/
