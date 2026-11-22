.PHONY: install link-skills test check clean

install:
	python3 -m pip install -e .

check:
	python3 -m py_compile agent_pair_ipc/*.py bin/agent-pair-ipc bin/claude-ipc bin/codex-ipc bin/antigravity-ipc skills/*/scripts/*.py

test: check
	python3 -m unittest discover -s tests -v
	./bin/agent-pair-ipc --version
	./bin/agent-pair-ipc list

# Point every agent's skill dir at the peer skills it calls in this repo.
# A pre-existing real directory (stale copy) is moved to ~/.agent-pair/backups/ first;
# plain `ln -sfn` would otherwise nest the link *inside* that directory.
SKILL_BACKUP_DIR := $(HOME)/.agent-pair/backups/skills-$(shell date +%Y%m%d%H%M%S)
SKILL_LINK_SPECS := \
	$(HOME)/.gemini/config/skills:claude-code-ipc,codex-ipc \
	$(HOME)/.claude/skills:antigravity-ipc,codex-ipc \
	$(HOME)/.agents/skills:claude-code-ipc,codex-ipc,antigravity-ipc

link-skills:
	@for spec in $(SKILL_LINK_SPECS); do \
		dir="$${spec%%:*}"; \
		skills="$${spec#*:}"; \
		mkdir -p "$$dir"; \
		old_ifs="$$IFS"; IFS=","; \
		for skill in $$skills; do \
			IFS="$$old_ifs"; \
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
		IFS="$$old_ifs"; \
	done

clean:
	rm -rf build dist *.egg-info __pycache__ agent_pair_ipc/__pycache__
