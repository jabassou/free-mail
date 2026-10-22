# Free Mail - common tasks.  `make` (or `make help`) lists them.
SHELL       := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

VENV   ?= .venv
PY     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
REPO   ?= jabassou/free-mail
VERSION_FILE := VERSION
CUR    := $(shell cat $(VERSION_FILE) 2>/dev/null)

# ------------------------------------------------------------------ setup
.PHONY: help
help: ## Show this help
	@awk 'BEGIN{FS=":.*## "} /^# ---/{sub(/^# -+ */,""); printf "\n\033[1m%s\033[0m\n",$$0} /^[a-zA-Z0-9_-]+:.*## /{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)
	@echo; echo "  version: $(CUR)   branch: $$(git branch --show-current 2>/dev/null)"

$(PY):
	@if [ -e $(VENV) ]; then echo "$(VENV) exists but $(PY) does not run: make reset-venv"; exit 1; fi
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip

STAMP := $(VENV)/.installed

$(STAMP): requirements.txt | $(PY)
	$(PIP) install -q -r requirements.txt
	@touch $@

.PHONY: install
install: $(STAMP) ## Create the venv and install runtime dependencies (skipped when up to date)

.PHONY: config
config: ## Create config.yaml and ask for the Free address when missing
	@[ -f config.yaml ] || cp config.example.yaml config.yaml
	@if ! grep -Eq '^user:[[:space:]]*[^[:space:]#]+@' config.yaml; then \
	  read -rp "Free address (e.g. john@free.fr): " u; [ -n "$$u" ] || exit 1; \
	  if grep -q '^user:' config.yaml; then sed -i.bak "s|^user:.*|user: $$u|" config.yaml && rm -f config.yaml.bak; \
	  else printf 'user: %s\n' "$$u" >> config.yaml; fi; echo "config.yaml: user = $$u"; fi

.PHONY: install-dev
install-dev: install ## + test dependencies and Playwright Chromium
	$(PIP) install -q -r tests/requirements.txt
	$(PY) -m playwright install chromium

.PHONY: reset-venv
reset-venv: ## Rebuild the venv from scratch (after a Python upgrade)
	rm -rf $(VENV)
	$(MAKE) install

# ------------------------------------------------------------------ run
.PHONY: self-update
self-update: ## Fast-forward main to origin/main when safe (clean tree, right repo); UPDATE=0 to skip
	@[ "$(UPDATE)" = "0" ] && exit 0; \
	git rev-parse --git-dir >/dev/null 2>&1 || exit 0; \
	url=$$(git remote get-url origin 2>/dev/null); \
	case "$$url" in *"$(REPO)"*) ;; *) echo "update: origin is not $(REPO), skipped"; exit 0;; esac; \
	[ "$$(git branch --show-current)" = "main" ] || { echo "update: not on main, skipped"; exit 0; }; \
	timeout 10 git fetch -q origin main --tags 2>/dev/null || { echo "update: offline, skipped"; exit 0; }; \
	n=$$(git rev-list --count HEAD..origin/main); \
	[ "$$n" -gt 0 ] || { echo "update: up to date (v$$(cat $(VERSION_FILE)))"; exit 0; }; \
	[ -z "$$(git status --porcelain --untracked-files=no)" ] || { echo "update: $$n new commit(s) but local changes present, skipped"; exit 0; }; \
	old=$$(cat $(VERSION_FILE)); git merge -q --ff-only origin/main \
	  && echo "update: v$$old -> v$$(cat $(VERSION_FILE)) ($$n commit(s))" \
	  || echo "update: fast-forward failed, skipped"

.PHONY: start
start: self-update install config ## Update from origin/main if safe, install if needed, ask for the password if none is stored, start the UI
	@$(PY) -c 'import freemail as f; f.get_password(f.load_config("config.yaml")["user"])' >/dev/null 2>&1 \
	  || { echo "No stored password: running auth"; $(PY) freemail.py auth; }
	$(PY) webui.py

.PHONY: ui
ui: $(PY) ## Start the local web UI (opens the browser)
	$(PY) webui.py

.PHONY: ui-bg
ui-bg: $(PY) ## Web UI without opening the browser, with background notifications (log)
	$(PY) webui.py --no-browser --imap-session --notify log

.PHONY: auth
auth: $(PY) ## Store the Free password in the Keychain and test IMAP
	$(PY) freemail.py auth

.PHONY: login
login: $(PY) ## Test the automatic webmail (Zimbra) login
	$(PY) freemail.py login

.PHONY: folders
folders: $(PY) ## List folders with counts
	$(PY) freemail.py folders

.PHONY: report
report: $(PY) ## Read-only mailbox statistics -> out/report-*/
	$(PY) freemail.py report

.PHONY: clean-dry
clean-dry: $(PY) ## Cleanup rules (rules.yaml), dry-run
	$(PY) freemail.py clean

.PHONY: clean-apply
clean-apply: $(PY) ## Cleanup rules, apply (asks for confirmation; RULE=name to run one)
	$(PY) freemail.py clean $(if $(RULE),--only $(RULE)) --apply

# ------------------------------------------------------------------ server filters (Zimbra)
.PHONY: filters-pull
filters-pull: $(PY) ## Back up the server filters -> backups/
	$(PY) freemail.py filters pull

.PHONY: filters-plan
filters-plan: $(PY) ## Diff filters.yaml against the server
	$(PY) freemail.py filters plan

.PHONY: filters-push
filters-push: $(PY) ## Push filters.yaml (auto backup first)
	$(PY) freemail.py filters push --apply

# ------------------------------------------------------------------ quality
.PHONY: test
test: $(PY) ## Full test suite (IMAP/UI tests are skipped without a local Dovecot)
	$(PY) -m pytest -q -p no:cacheprovider

.PHONY: test-fast
test-fast: $(PY) ## Unit and static tests only (no mail server needed)
	$(PY) -m pytest -q -p no:cacheprovider tests/test_extras.py tests/test_static.py tests/test_security.py

.PHONY: lint
lint: $(PY) ## Compile the Python code and parse the service worker
	$(PY) -m py_compile freemail.py mailweb.py webui.py extras.py
	@command -v node >/dev/null && node --check web/sw.js || echo "node not installed: sw.js not checked"

# ------------------------------------------------------------------ android
.PHONY: apk
apk: ## Build the signed APK locally -> FreeMail.apk
	cd android && ./build-apk.sh

.PHONY: apk-install
apk-install: ## Build and install on the phone (adb, USB or Wi-Fi debugging)
	cd android && ./build-apk.sh --install

.PHONY: signing-backup
signing-backup: ## Encrypted backup of the signing key (~/Documents)
	cd android && ./signing-backup.sh

.PHONY: termux-bundle
termux-bundle: ## Build free-mail-termux.zip (legacy Termux mode)
	./termux/bundle.sh

# ------------------------------------------------------------------ manual release (V=X.Y.Z, without the GitHub Action)
.PHONY: release-pr
release-pr: ## 1/3 Bump VERSION on a release branch, PR into dev, merge when green
	@[ -n "$(V)" ] || { echo "usage: make release-pr V=X.Y.Z"; exit 1; }
	@git remote get-url origin | grep -q "$(REPO)" || { echo "wrong repository"; exit 1; }
	@git diff --quiet && git diff --cached --quiet || { echo "working tree not clean"; exit 1; }
	git fetch -q origin
	git switch -c release/$(V) origin/dev
	echo "$(V)" > $(VERSION_FILE)
	git commit -qam "chore: release $(V)"
	git push -u origin release/$(V)
	gh pr create --base dev --title "chore: release $(V)" --body "Release $(V)."
	sleep 20; gh pr checks release/$(V) --watch
	gh pr merge release/$(V) --squash --delete-branch
	git switch dev && git pull -q

.PHONY: promote
promote: ## 2/3 PR dev -> main, merged by number when green (merge commit)
	@git remote get-url origin | grep -q "$(REPO)" || { echo "wrong repository"; exit 1; }
	gh pr create --base main --head dev --title "Release $$(git show origin/dev:VERSION)" --body "Release $$(git show origin/dev:VERSION)." || true
	@PR=$$(gh pr list --base main --head dev --state open --json number --jq '.[0].number'); \
	  [ -n "$$PR" ] || { echo "no open dev -> main PR"; exit 1; }; echo "PR #$$PR"; \
	  sleep 20; gh pr checks "$$PR" --watch; gh pr merge "$$PR" --merge; \
	  [ "$$(gh pr view "$$PR" --json state --jq .state)" = MERGED ] || { echo "PR #$$PR not merged"; exit 1; }

.PHONY: tag
tag: ## 3/3 Tag main (only if VERSION matches V) -> the CI builds and publishes the APK
	@[ -n "$(V)" ] || { echo "usage: make tag V=X.Y.Z"; exit 1; }
	@git remote get-url origin | grep -q "$(REPO)" || { echo "wrong repository"; exit 1; }
	git switch main && git pull -q
	@[ "$$(cat $(VERSION_FILE))" = "$(V)" ] || { echo "STOP: main is $$(cat $(VERSION_FILE)), not $(V)"; git switch -q dev; exit 1; }
	git tag v$(V) && git push origin v$(V)
	sleep 15; gh run watch "$$(gh run list --workflow apk --branch v$(V) --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
	gh release view v$(V) --json assets --jq '[.assets[].name]'
	git switch dev

.PHONY: release-manual
release-manual: ## Manual full release without the Action: release-pr + promote + tag (V=X.Y.Z)
	$(MAKE) release-pr V=$(V)
	$(MAKE) promote
	$(MAKE) tag V=$(V)

# ------------------------------------------------------------------ automated release (GitHub Action "release")
# Runs .github/workflows/release.yml from main: computes the next version, bumps dev,
# promotes dev -> main, tags, waits for the APK. You only approve the "release" deployment.
.PHONY: release
release: ## Automated release, approves the deployment for you; bump guessed from commit titles (BUMP=minor to force, APPROVE=0 to approve in the UI)
	@git remote get-url origin | grep -q "$(REPO)" || { echo "wrong repository"; exit 1; }
	@git fetch -q origin
	@SUBJ=$$(git log --format=%s origin/main..origin/dev); \
	  [ -n "$$SUBJ" ] || { echo "dev has nothing new compared to main"; exit 1; }; \
	  echo "Commits to release:"; echo "$$SUBJ" | sed 's/^/  - /'; \
	  if [ -n "$(BUMP)" ]; then B="$(BUMP)"; \
	  elif echo "$$SUBJ" | grep -Eq '^[a-z]+(\([^)]*\))?!:|BREAKING'; then B=major; \
	  elif echo "$$SUBJ" | grep -Eq '^feat(\([^)]*\))?:'; then B=minor; \
	  else B=patch; fi; \
	  echo "Suggested bump: $$B"; \
	  $(MAKE) --no-print-directory _release-run BUMP=$$B

.PHONY: _release-run
_release-run:
	@gh repo view "$(REPO)" >/dev/null || { echo "cannot reach $(REPO)"; exit 1; }
	@git remote get-url origin | grep -q "$(REPO)" || { echo "wrong repository"; exit 1; }
	@echo "Next: $$(git show origin/main:VERSION 2>/dev/null | tr -d '[:space:]') -> bump $(BUMP)"
	@read -rp "Start the $(BUMP) release? [y/N] " a; [ "$$a" = y ] || { echo aborted; exit 1; }
	gh workflow run release.yml -R $(REPO) --ref main -f bump=$(BUMP)
	@for i in $$(seq 1 20); do \
	  ID=$$(gh run list -R $(REPO) --workflow release.yml --event workflow_dispatch --limit 5 --json databaseId,status --jq '[.[]|select(.status!="completed")][0].databaseId // empty'); \
	  [ -n "$$ID" ] && break; sleep 3; done; \
	  [ -n "$$ID" ] || { echo "run not found: check the Actions tab"; exit 1; }; \
	  echo "Run $$ID"; \
	  if [ "$(APPROVE)" = "0" ]; then echo "Approve the 'release' deployment in the GitHub UI"; \
	  else \
	    for i in $$(seq 1 30); do \
	      ENV=$$(gh api repos/$(REPO)/actions/runs/$$ID/pending_deployments --jq '.[0].environment.id // empty' 2>/dev/null); \
	      [ -n "$$ENV" ] && break; sleep 3; done; \
	    if [ -n "$$ENV" ]; then \
	      gh api -X POST repos/$(REPO)/actions/runs/$$ID/pending_deployments -F "environment_ids[]=$$ENV" -f state=approved -f comment="make release" >/dev/null \
	        && echo "Deployment approved"; \
	    else echo "No pending approval found (already approved?)"; fi; \
	  fi; \
	  gh run watch -R $(REPO) "$$ID" --exit-status

.PHONY: release-patch
release-patch: ## Automated release, patch bump (1.6.0 -> 1.6.1)
	@$(MAKE) --no-print-directory _release-run BUMP=patch

.PHONY: release-minor
release-minor: ## Automated release, minor bump (1.6.0 -> 1.7.0)
	@$(MAKE) --no-print-directory _release-run BUMP=minor

.PHONY: release-major
release-major: ## Automated release, major bump (1.6.0 -> 2.0.0)
	@$(MAKE) --no-print-directory _release-run BUMP=major

# ------------------------------------------------------------------ misc
.PHONY: status
status: ## Branch, version, open PRs and latest release
	@echo "branch: $$(git branch --show-current)   VERSION: $(CUR)"
	@gh pr list --limit 10
	@gh release view --json tagName,publishedAt --jq '"latest release: \(.tagName) (\(.publishedAt))"'

.PHONY: clean
clean: ## Remove caches and build outputs (keeps config, rules, backups)
	rm -rf __pycache__ */__pycache__ .pytest_cache FreeMail.apk free-mail-termux.zip
