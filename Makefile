.PHONY: build install clean pull push sync status help

REMINDERS_CLI_REPO := https://github.com/keith/reminders-cli.git
REMINDERS_CLI_DIR  := vendor/reminders-cli
BIN                := bin/reminders-cli

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build: $(BIN)  ## Build the bundled reminders-cli Swift binary

$(BIN):
	@mkdir -p vendor
	@if [ ! -d "$(REMINDERS_CLI_DIR)" ]; then \
		echo "Cloning keith/reminders-cli..."; \
		git clone --depth 1 $(REMINDERS_CLI_REPO) $(REMINDERS_CLI_DIR); \
	fi
	@echo "Building Swift binary (release)..."
	@cd $(REMINDERS_CLI_DIR) && swift build -c release
	@cp $(REMINDERS_CLI_DIR)/.build/release/reminders $(BIN)
	@chmod +x $(BIN)
	@echo "✓ Built $(BIN)"

install: build  ## Build + symlink wrappers into ~/.local/bin
	@mkdir -p $$HOME/.local/bin
	@for w in todo-pull todo-push todo-sync todo-status; do \
		ln -sf "$(PWD)/bin/$$w" "$$HOME/.local/bin/$$w"; \
		echo "✓ symlinked ~/.local/bin/$$w"; \
	done
	@echo
	@echo "Make sure ~/.local/bin is on your PATH."

pull:  ## Reminders → TASKS.md
	@python3 bin/sync.py pull

push:  ## TASKS.md → Reminders
	@python3 bin/sync.py push

sync:  ## bidirectional
	@python3 bin/sync.py sync

status:  ## read-only summary
	@python3 bin/sync.py status

clean:  ## Remove built binary and vendor checkout
	rm -f $(BIN)
	rm -rf vendor/
