.DEFAULT_GOAL := help

.PHONY: help build release test fmt fmt-check lint check clean install

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} \
	/^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

build:  ## Debug build (target/debug/rp)
	cargo build

release:  ## Optimised build (target/release/rp)
	cargo build --release

test:  ## Unit + end-to-end tests
	cargo test

fmt:  ## Format the sources
	cargo fmt

fmt-check:  ## Fail if the sources are not formatted
	cargo fmt --check

lint:  ## Clippy with warnings as errors
	cargo clippy --all-targets -- -D warnings

install:  ## Install `rp` into ~/.cargo/bin
	cargo install --path . --locked

clean:  ## Remove build output
	cargo clean

# The full deterministic gate, identical to CI.
check:  ## Full gate: format, lint, tests
check: fmt-check lint test
	@echo "all checks passed"
