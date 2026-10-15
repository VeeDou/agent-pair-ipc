.PHONY: install link-skills test check clean

install:
	python3 -m pip install -e .

check:
	python3 -m py_compile agent_pair_ipc/*.py bin/agent-pair-ipc bin/claude-ipc bin/codex-ipc

test: check
	python3 -m unittest discover -s tests -v
	./bin/agent-pair-ipc --version
	./bin/agent-pair-ipc list

link-skills:
	@mkdir -p ~/.gemini/config/skills ~/.agents/skills
	@ln -sfn $(CURDIR)/skills/claude-code-ipc ~/.gemini/config/skills/claude-code-ipc
	@ln -sfn $(CURDIR)/skills/codex-ipc ~/.gemini/config/skills/codex-ipc
	@ln -sfn $(CURDIR)/skills/claude-code-ipc ~/.agents/skills/claude-code-ipc
	@ln -sfn $(CURDIR)/skills/codex-ipc ~/.agents/skills/codex-ipc
	@echo "Skills successfully linked to ~/.gemini/config/skills and ~/.agents/skills"

clean:
	rm -rf build dist *.egg-info __pycache__ agent_pair_ipc/__pycache__
