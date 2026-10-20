# Build, test and release the sample project.
# Run `make help` for the list of targets.

ROOT_DIR := $(dir $(lastword $(MAKEFILE_LIST)))
PYTHON ?= python3
OUT_DIR = $(CURDIR)/bin
export GOFLAGS = -mod=mod

include common.mk

.PHONY: all build test release help

all: build test

build: $(OUT_DIR) ## Compile the binaries
	./scripts/build.sh --out $(OUT_DIR)

test: build | $(OUT_DIR)
	bash scripts/test.sh
	$(PYTHON) tools/gen.py --check

# Tag the release and publish the artifacts.
release: test
	@$(MAKE) build GOFLAGS=-trimpath
	$(MAKE) -C docs html

$(OUT_DIR):
	mkdir -p $@

define HELP_TEXT
Targets: all build test release
endef

ifeq ($(CI),true)
ci: all
	$(ROOT_DIR)/scripts/test.sh --ci
endif

help:
	@echo "$(HELP_TEXT)"
