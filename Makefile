.PHONY: install link-skills test check clean

install:
	python3 -m pip install -e .

check:
	python3 -m py_compile agent_pair_ipc/*.py bin/agent-pair-ipc bin/claude-ipc bin/codex-ipc skills/*/scripts/*.py

test: check
	python3 -m unittest discover -s tests -v
	./bin/agent-pair-ipc --version
	./bin/agent-pair-ipc list

# Point every agent's skill dir at this repo. A pre-existing real directory (stale copy) is moved to
# ~/.agent-pair/backups/ first; plain `ln -sfn` would otherwise nest the link *inside* that directory.
SKILL_TARGETS := $(HOME)/.agents/skills $(HOME)/.gemini/config/skills
SKILL_BACKUP_DIR := $(HOME)/.agent-pair/backups/skills-$(shell date +%Y%m%d%H%M%S)

link-skills:
	@for dir in $(SKILL_TARGETS); do \
		mkdir -p "$$dir"; \
		for skill in claude-code-ipc codex-ipc; do \
			dest="$$dir/$$skill"; \
			if [ -e "$$dest" ] && [ ! -L "$$dest" ]; then \
				backup="$(SKILL_BACKUP_DIR)/$$(basename $$(dirname $$dir) | sed 's/^\.//')"; \
				mkdir -p "$$backup"; \
				mv "$$dest" "$$backup/$$skill"; \
				echo "Backed up stale $$dest -> $$backup/$$skill"; \
			fi; \
			ln -sfn "$(CURDIR)/skills/$$skill" "$$dest"; \
			echo "Linked $$dest -> $(CURDIR)/skills/$$skill"; \
		done; \
	done

clean:
	rm -rf build dist *.egg-info __pycache__ agent_pair_ipc/__pycache__
