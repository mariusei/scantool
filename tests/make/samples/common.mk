# Shared rules for every makefile of the project.
VERSION := $(shell ./scripts/version.sh)

clean:
	rm -rf bin
