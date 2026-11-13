# Source before scripts/cloud/stack.sh start to enable the fixture connectors
# (tests/fixtures/connectors; never shipped). Starts nothing by itself.
#   source scripts/cloud/checks/apps/connector-fixtures.env.sh
_root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
export CLIO_CONNECTOR_CATALOG_DIRS="$_root/tests/fixtures/connectors"
export CLIO_CONNECTOR_FIXTURE_DIR="$_root/tests/fixtures"
export CLIO_CONNECTOR_FIXTURE_PYTHON="$_root/.venv/bin/python"
unset _root
