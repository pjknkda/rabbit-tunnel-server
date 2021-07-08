.PHONY: all
all:


.PHONY: update_requirements
update_requirements:
	hash pip-compile > /dev/null || (echo "You need to install pip-tools first!" && exit 1)
	rm requirements.txt > /dev/null 2>&1 || true
	pip-compile --generate-hashes --no-header -o requirements.txt