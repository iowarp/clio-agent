# UV Package Manager Guide
> Version: uv (latest) | Updated: February 2026

Comprehensive reference for UV package manager in CLIO Agent development. UV is an extremely fast Python package installer and resolver written in Rust by Astral.

## Table of Contents

- [Installation](#installation)
- [Project Creation](#project-creation)
- [Dependency Management](#dependency-management)
- [Locking and Syncing](#locking-and-syncing)
- [Running Code](#running-code)
- [Scripts with PEP 723 Inline Metadata](#scripts-with-pep-723-inline-metadata)
- [Tool Management](#tool-management)
- [Python Version Management](#python-version-management)
- [Workspaces](#workspaces)
- [Building and Publishing](#building-and-publishing)
- [Index Configuration](#index-configuration)
- [Project Configuration](#project-configuration)
- [CLIO-Specific Patterns](#clio-specific-patterns)

---

## Installation

### Quick Install

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# With pip
pip install uv

# With Homebrew (macOS)
brew install uv

# With cargo
cargo install --git https://github.com/astral-sh/uv uv
```

### Verify Installation

```bash
uv --version
# uv 0.5.0 (or later)
```

---

## Project Creation

### Initialize New Project

```bash
# Create application project (with src/ layout)
uv init --app my-app
cd my-app

# Create library project
uv init --lib my-library

# Create package with build backend
uv init --package my-package

# Bare project (no src/ layout)
uv init --bare simple-project

# Specify build backend
uv init --build-backend hatchling my-project
uv init --build-backend setuptools my-project
```

### Project Structure

**Application (--app):**
```
my-app/
├── pyproject.toml
├── README.md
├── .python-version
└── src/
    └── my_app/
        └── __init__.py
```

**Library (--lib):**
```
my-library/
├── pyproject.toml
├── README.md
├── .python-version
├── src/
│   └── my_library/
│       └── __init__.py
└── tests/
    └── __init__.py
```

### Manual pyproject.toml Setup

```toml
[project]
name = "my-project"
version = "0.1.0"
description = "My awesome project"
requires-python = ">=3.12"
dependencies = [
    "requests>=2.31.0",
    "rich>=14.0.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

---

## Dependency Management

### Adding Dependencies

```bash
# Add production dependency
uv add requests

# Add with version constraint
uv add "requests>=2.31.0"
uv add "requests~=2.31.0"  # Compatible release
uv add "requests==2.31.0"  # Exact version

# Add multiple dependencies
uv add requests rich click

# Add to optional dependency group
uv add --optional dev pytest pytest-cov
uv add --optional docs sphinx sphinx-rtd-theme

# Add development dependencies
uv add --dev pytest ruff mypy
uv add --dev --editable .

# Add with extras
uv add "fastapi[standard]"
uv add "dspy-ai[openai,anthropic]"
```

### Dependency Groups

```toml
[project.optional-dependencies]
# UI dependencies
ui = [
    "rich>=14.2.0",
    "prompt-toolkit>=3.0.0",
]

# Memory layer dependencies
memory = [
    "sortedcontainers>=2.4.0",
    "lru-dict>=1.3.0",
    "msgspec>=0.18.0",
]

# Development dependencies
dev = [
    "pytest>=7.4.0",
    "pytest-cov>=4.1.0",
    "ruff>=0.1.0",
    "mypy>=1.7.0",
]

# All optional dependencies
all = [
    "my-project[ui,memory,dev]",
]
```

### Installing Optional Dependencies

```bash
# Install specific group
uv sync --extra ui
uv sync --extra memory

# Install multiple groups
uv sync --extra ui --extra memory

# Install all optional dependencies
uv sync --all-extras

# Install with dev dependencies
uv sync --extra dev
```

### Removing Dependencies

```bash
# Remove dependency
uv remove requests

# Remove from optional group
uv remove --optional dev pytest

# Remove development dependency
uv remove --dev mypy
```

---

## Advanced Dependency Sources

### tool.uv.sources

Define non-PyPI dependency sources in pyproject.toml:

```toml
[tool.uv.sources]
# Git repository
my-package = { git = "https://github.com/user/my-package.git" }

# Git with branch
my-package = { git = "https://github.com/user/my-package.git", branch = "main" }

# Git with tag
my-package = { git = "https://github.com/user/my-package.git", tag = "v1.0.0" }

# Git with commit
my-package = { git = "https://github.com/user/my-package.git", rev = "abc123" }

# Local path (absolute)
my-local-pkg = { path = "/absolute/path/to/package" }

# Local path (relative to pyproject.toml)
my-local-pkg = { path = "./packages/my-local-pkg" }

# Editable local path
my-local-pkg = { path = "./packages/my-local-pkg", editable = true }

# Direct URL
my-package = { url = "https://example.com/my-package-1.0.0.tar.gz" }

# Workspace member
my-workspace-pkg = { workspace = true }

# Custom index
my-package = { index = "my-private-index" }

# Platform-specific dependencies
windows-only = { path = "./windows-deps", markers = "sys_platform == 'win32'" }
linux-only = { path = "./linux-deps", markers = "sys_platform == 'linux'" }
```

### Using Custom Sources

```bash
# Add from git
uv add "my-package @ git+https://github.com/user/my-package.git"

# Add from git with branch
uv add "my-package @ git+https://github.com/user/my-package.git@main"

# Add from local path
uv add "my-package @ file:///path/to/package"
uv add "my-package @ ./relative/path/to/package"

# Add as editable
uv add --editable ./path/to/package
```

---

## Locking and Syncing

### Lock File Management

```bash
# Generate/update lock file (uv.lock)
uv lock

# Lock without updating existing locked versions
uv lock --frozen

# Upgrade all dependencies
uv lock --upgrade

# Upgrade specific package
uv lock --upgrade-package requests

# Dry run (show what would change)
uv lock --dry-run
```

### Syncing Environment

```bash
# Sync virtual environment with lock file
uv sync

# Use locked versions (fail if lock file is stale)
uv sync --locked

# Use locked versions (don't update lock file)
uv sync --frozen

# Sync only production dependencies (no dev)
uv sync --no-dev

# Sync with specific extras
uv sync --extra ui --extra memory

# Sync all optional dependencies
uv sync --all-extras
```

### Lock File Export

```bash
# Export to requirements.txt format
uv export --format requirements-txt > requirements.txt

# Export with hashes
uv export --format requirements-txt --all-extras --hash > requirements-lock.txt

# Export specific extras
uv export --format requirements-txt --extra ui > requirements-ui.txt
```

---

## Running Code

### Basic Execution

```bash
# Run Python script in project environment
uv run python script.py

# Run Python command
uv run python -c "import sys; print(sys.version)"

# Run module
uv run python -m pytest

# Run installed script (from project.scripts)
uv run my-script

# Run with arguments
uv run my-script --flag value arg1 arg2
```

### Temporary Dependencies

```bash
# Run with additional temporary dependency
uv run --with requests python script.py

# Run with multiple temporary dependencies
uv run --with requests --with rich python script.py

# Run with specific version
uv run --with "requests>=2.31.0" python script.py
```

### Running Without Project

```bash
# Run script without project context
uv run --no-project script.py

# Run with temporary dependencies (no project)
uv run --no-project --with requests python -c "import requests; print(requests.__version__)"
```

### Python Version Selection

```bash
# Run with specific Python version
uv run --python 3.12 script.py
uv run --python 3.11 python -c "import sys; print(sys.version)"

# Run with python from PATH
uv run --python python3.12 script.py
```

---

## Scripts with PEP 723 Inline Metadata

### Basic Script Structure

PEP 723 allows embedding dependencies directly in Python scripts:

```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "requests>=2.31.0",
#   "rich>=14.0.0",
# ]
# ///

"""
Standalone script with inline dependencies.
UV automatically creates isolated environment.
"""

import requests
from rich import print

def main():
    response = requests.get("https://api.github.com")
    print(f"[bold green]Status:[/bold green] {response.status_code}")

if __name__ == "__main__":
    main()
```

### Script Metadata Syntax

```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "dspy-ai>=3.0.3",
#   "fastmcp>=2.13.0",
#   "rich>=14.2.0",
# ]
# ///
```

**Key Requirements:**
- Must start with `# /// script` (exactly)
- Must end with `# ///` (exactly)
- TOML format between markers
- Each line must start with `#`

### Advanced Script Examples

**With Click CLI:**
```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click>=8.1.0",
#   "requests>=2.31.0",
# ]
# ///

import click
import requests

@click.command()
@click.option('--url', required=True, help='URL to fetch')
@click.option('--timeout', default=10, help='Request timeout')
def fetch(url: str, timeout: int):
    """Fetch URL and print status."""
    response = requests.get(url, timeout=timeout)
    click.echo(f"Status: {response.status_code}")
    click.echo(f"Length: {len(response.content)} bytes")

if __name__ == "__main__":
    fetch()
```

**With DSPy Agent:**
```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "dspy-ai>=3.0.3",
# ]
# ///

import dspy

def main():
    # Configure DSPy
    lm = dspy.LM('ollama_chat/llama3.2:3b', api_base='http://localhost:11434')
    dspy.configure(lm=lm)

    # Simple prediction
    class QA(dspy.Signature):
        """Answer questions with short factoid answers."""
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()

    predictor = dspy.Predict(QA)
    result = predictor(question="What is the capital of France?")
    print(f"Answer: {result.answer}")

if __name__ == "__main__":
    main()
```

### Running Scripts

```bash
# Run script (UV handles dependencies automatically)
uv run my_script.py

# Run with arguments
uv run my_script.py --url https://example.com

# Make executable and run directly
chmod +x my_script.py
./my_script.py

# Run without uv (if dependencies already installed)
python my_script.py
```

### Script Best Practices

1. **Always specify Python version:** `requires-python = ">=3.12"`
2. **Pin major versions:** `"requests>=2.31.0"` not `"requests"`
3. **Keep scripts focused:** One responsibility per script
4. **Use type hints:** Better IDE support and documentation
5. **Add docstrings:** Explain what the script does
6. **Handle errors:** Proper exception handling for robustness

---

## Tool Management

### Running Tools (uvx / uv tool run)

```bash
# Run tool temporarily (don't install)
uvx ruff check .
uvx black .
uvx pytest

# Alternative syntax
uv tool run ruff check .

# Run with specific version
uvx ruff@0.1.0 check .

# Run tool from git
uvx --from git+https://github.com/user/tool mytool

# Run with extra dependencies
uvx --with requests --with rich mytool
```

### Installing Tools

```bash
# Install tool globally
uv tool install ruff
uv tool install black
uv tool install pytest

# Install specific version
uv tool install ruff@0.1.0

# Install from git
uv tool install --from git+https://github.com/user/tool mytool

# Install with extras
uv tool install "black[jupyter]"
```

### Managing Installed Tools

```bash
# List installed tools
uv tool list

# Upgrade tool
uv tool upgrade ruff
uv tool upgrade --all

# Uninstall tool
uv tool uninstall ruff

# Show tool info
uv tool show ruff
```

### Tool Use Cases

**Formatters and Linters:**
```bash
uvx ruff check . --fix
uvx black .
uvx isort .
uvx mypy src/
```

**Build and Packaging:**
```bash
uvx build  # Build wheel and sdist
uvx twine upload dist/*
```

**Project Templates:**
```bash
uvx cookiecutter gh:audreyr/cookiecutter-pypackage
```

**One-off Scripts:**
```bash
uvx httpie https://api.github.com
uvx rich-cli README.md
```

---

## Python Version Management

### Installing Python Versions

```bash
# Install specific Python version
uv python install 3.12
uv python install 3.11.7

# Install latest stable
uv python install 3.12

# Install multiple versions
uv python install 3.11 3.12 3.13

# Install from python.org, deadsnakes, etc.
uv python install 3.12 --python-source python-build-standalone
```

### Listing Python Versions

```bash
# List installed Python versions
uv python list

# List available Python versions (remote)
uv python list --all

# Find Python versions on system
uv python find
uv python find 3.12
```

### Pinning Python Version

```bash
# Pin Python version for project
uv python pin 3.12

# Pin specific version
uv python pin 3.12.1

# Creates .python-version file
cat .python-version
# 3.12
```

**.python-version file:**
```
3.12
```

### Upgrading Python

```bash
# Upgrade to latest patch version
uv python upgrade

# Upgrade specific version
uv python upgrade 3.12
```

---

## Workspaces

Workspaces allow managing multiple related packages in a single repository (monorepo).

### Workspace Structure

```
my-workspace/
├── pyproject.toml          # Root workspace config
├── uv.lock                 # Single lock file for all packages
├── packages/
│   ├── core/
│   │   ├── pyproject.toml
│   │   └── src/core/
│   ├── cli/
│   │   ├── pyproject.toml
│   │   └── src/cli/
│   └── api/
│       ├── pyproject.toml
│       └── src/api/
```

### Root pyproject.toml

```toml
[tool.uv.workspace]
members = [
    "packages/core",
    "packages/cli",
    "packages/api",
]

# Or use glob patterns
members = ["packages/*"]

# Exclude specific paths
exclude = ["packages/experimental"]
```

### Workspace Member pyproject.toml

```toml
[project]
name = "my-workspace-cli"
version = "0.1.0"
dependencies = [
    "click>=8.1.0",
]

[tool.uv.sources]
# Reference workspace member
my-workspace-core = { workspace = true }

[project.dependencies]
my-workspace-core = "*"  # Version managed by workspace
```

### Working with Workspaces

```bash
# Initialize workspace
mkdir my-workspace && cd my-workspace
uv init --lib

# Add workspace configuration to pyproject.toml
# [tool.uv.workspace]
# members = ["packages/*"]

# Create workspace members
mkdir -p packages/core packages/cli
cd packages/core && uv init --lib core
cd ../cli && uv init --app cli

# Add dependencies in workspace
uv add requests  # Adds to current package
uv add --package core rich  # Adds to specific package

# Sync entire workspace
uv sync

# Lock workspace
uv lock
```

### Workspace Benefits

- **Single lock file:** All packages use same dependency versions
- **Cross-references:** Easy imports between packages
- **Unified testing:** Run tests across all packages
- **Simplified CI/CD:** Build and test entire workspace together

---

## Building and Publishing

### Building Packages

```bash
# Build wheel and source distribution
uv build

# Build only wheel
uv build --wheel

# Build only source distribution
uv build --sdist

# Build to specific directory
uv build --out-dir dist/

# Build with specific backend
uv build --backend hatchling
```

**Build outputs:**
```
dist/
├── my_package-0.1.0-py3-none-any.whl
└── my_package-0.1.0.tar.gz
```

### Version Bumping

```bash
# Bump version (requires build backend support)
uv version --bump patch   # 0.1.0 -> 0.1.1
uv version --bump minor   # 0.1.0 -> 0.2.0
uv version --bump major   # 0.1.0 -> 1.0.0

# Set specific version
uv version 1.2.3
```

### Publishing to PyPI

```bash
# Build first
uv build

# Publish to PyPI (requires credentials)
uv publish

# Publish to Test PyPI
uv publish --index-url https://test.pypi.org/legacy/ --token <token>

# Publish specific files
uv publish dist/my_package-0.1.0-py3-none-any.whl

# Dry run (check without uploading)
uv publish --dry-run
```

### Configure PyPI Credentials

**Option 1: Environment variables**
```bash
export UV_PUBLISH_TOKEN="pypi-..."
uv publish
```

**Option 2: pyproject.toml**
```toml
[tool.uv.publish]
index-url = "https://upload.pypi.org/legacy/"
token = "${UV_PUBLISH_TOKEN}"  # Read from environment
```

---

## Index Configuration

### Custom Package Indexes

```toml
[tool.uv]
# Default index (replaces PyPI)
index-url = "https://pypi.org/simple"

# Additional indexes
extra-index-url = [
    "https://download.pytorch.org/whl/cpu",
    "https://my-company.jfrog.io/artifactory/api/pypi/python/simple",
]

# Find links (for local packages)
find-links = [
    "file:///path/to/packages",
    "https://example.com/packages",
]
```

### Named Indexes

```toml
[[tool.uv.index]]
name = "pytorch"
url = "https://download.pytorch.org/whl/cpu"

[[tool.uv.index]]
name = "private"
url = "https://my-company.jfrog.io/artifactory/api/pypi/python/simple"
default = false  # Not used by default
```

### Explicit Index Assignment

```toml
[tool.uv.sources]
# Use specific index for package
torch = { index = "pytorch" }
my-private-lib = { index = "private" }
```

### Using Custom Indexes

```bash
# Add with custom index
uv add --index https://custom-index.com/simple my-package

# Use extra index
uv sync --extra-index-url https://custom-index.com/simple
```

---

## Project Configuration

### Complete pyproject.toml Reference

```toml
[project]
name = "my-project"
version = "0.1.0"
description = "My awesome project"
authors = [
    { name = "Your Name", email = "you@example.com" }
]
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.12"
keywords = ["keyword1", "keyword2"]
classifiers = [
    "Development Status :: 4 - Beta",
    "Programming Language :: Python :: 3.12",
]

dependencies = [
    "requests>=2.31.0",
    "rich>=14.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4.0",
    "ruff>=0.1.0",
]

[project.urls]
Homepage = "https://github.com/user/my-project"
Documentation = "https://my-project.readthedocs.io"
Repository = "https://github.com/user/my-project"
Issues = "https://github.com/user/my-project/issues"

[project.scripts]
my-cli = "my_project.cli:main"

[project.entry-points."my_plugin_group"]
my-plugin = "my_project.plugin:PluginClass"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.uv]
# UV-specific configuration
dev-dependencies = [
    "pytest>=7.4.0",
]

# Package sources
[tool.uv.sources]
my-dep = { git = "https://github.com/user/my-dep.git" }

# Custom indexes
[[tool.uv.index]]
name = "my-index"
url = "https://my-index.com/simple"

# Cache configuration
[tool.uv]
cache-keys = [{ file = "requirements.txt" }]

# Environment configuration
[tool.uv.environments]
python-version = "3.12"
platform = "linux"
```

### UV-Specific Settings

```toml
[tool.uv]
# Python version management
python-version = "3.12"
python-preference = "only-managed"  # or "managed", "system", "only-system"

# Resolution strategy
resolution = "highest"  # or "lowest", "lowest-direct"

# Pre-release handling
prerelease = "allow"  # or "disallow", "if-necessary"

# Index configuration
index-url = "https://pypi.org/simple"
extra-index-url = ["https://custom-index.com/simple"]

# Exclude packages from lock file
no-build = ["opencv-python"]
no-binary = ["grpcio"]

# Cache configuration
cache-dir = ".uv-cache"
compile-bytecode = true

# Concurrent downloads
concurrent-downloads = 10
concurrent-installs = 4
```

### Environment Markers

```toml
[project.dependencies]
# Platform-specific
windows-specific = "pywin32>=306; sys_platform == 'win32'"
linux-specific = "python-prctl>=1.8; sys_platform == 'linux'"

# Python version specific
typing-extensions = ">=4.0; python_version < '3.11'"

# Implementation specific
gevent = ">=23.0; platform_python_implementation == 'CPython'"
```

---

## CLIO-Specific Patterns

### CLIO Agent Project Structure

```toml
[project]
name = "clio-agent"
version = "0.2.0"
description = "CLIO Agent — Autonomous AI agent framework for scientific data"
requires-python = ">=3.12"

dependencies = [
    "dspy-ai>=3.0.3",      # Core agent framework (INTERNAL)
    "fastmcp>=2.13.0",     # MCP protocol
    "requests>=2.31.0",
]

[project.optional-dependencies]
ui = [
    "rich>=14.2.0",
    "prompt-toolkit>=3.0.0",
]

memory = [
    "sortedcontainers>=2.4.0",  # B-tree index
    "lru-dict>=1.3.0",           # LRU cache
    "msgspec>=0.18.0",           # Serialization
]

optimizers = [
    "scipy>=1.11.0",
    "numpy>=1.24.0",
]

tools = [
    "h5py>=3.10.0",              # HDF5 support
    "pyarrow>=14.0.0",           # Parquet support
]

dev = [
    "pytest>=7.4.0",
    "pytest-cov>=4.1.0",
    "pytest-asyncio>=0.21.0",
    "ruff>=0.1.0",
    "mypy>=1.7.0",
]

all = [
    "clio-agent[ui,memory,optimizers,tools,dev]",
]

[project.scripts]
clio-agent = "clio_agent.ui.cli:run_cli"
clio-agent-gact = "clio_agent.gact.app:main"
```

### Installing CLIO Agent

```bash
# Clone repository
git clone https://github.com/iowarp/clio-agent.git
cd clio-agent

# Install with all features
uv sync --all-extras

# Install specific features
uv sync --extra ui --extra memory

# Install for development
uv sync --all-extras

# Run CLIO Agent
uv run clio-agent
```

### Running CLIO Development Tasks

```bash
# Run CLI
uv run src/clio_agent/ui/cli.py

# Run tests
uv run pytest tests/

# Run linter
uvx ruff check src/

# Run type checker
uvx mypy src/clio_agent/

# Run with specific Python
uv run --python 3.12 src/clio_agent/ui/cli.py
```

### CLIO Script Examples

**Quick Test Script:**
```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "dspy-ai>=3.0.3",
#   "rich>=14.2.0",
# ]
# ///

"""Quick test of CLIO agent functionality."""

import dspy
from rich import print

def test_agent():
    lm = dspy.LM('ollama_chat/llama3.2:3b', api_base='http://localhost:11434')
    dspy.configure(lm=lm)

    class SimpleQA(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()

    agent = dspy.Predict(SimpleQA)
    result = agent(question="What is HDF5?")
    print(f"[bold green]Answer:[/bold green] {result.answer}")

if __name__ == "__main__":
    test_agent()
```

**Data Processing Script:**
```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "h5py>=3.10.0",
#   "numpy>=1.24.0",
#   "rich>=14.2.0",
# ]
# ///

"""Process HDF5 scientific data files."""

import h5py
import numpy as np
from rich import print

def analyze_hdf5(filepath: str):
    with h5py.File(filepath, 'r') as f:
        print(f"[bold]File:[/bold] {filepath}")
        print(f"[bold]Keys:[/bold] {list(f.keys())}")

        for key in f.keys():
            dataset = f[key]
            print(f"\n[bold]{key}:[/bold]")
            print(f"  Shape: {dataset.shape}")
            print(f"  Dtype: {dataset.dtype}")
            print(f"  Size: {dataset.size:,} elements")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("[red]Usage:[/red] uv run script.py <hdf5_file>")
        sys.exit(1)
    analyze_hdf5(sys.argv[1])
```

### CLIO Development Workflow

```bash
# 1. Make changes to code
vim src/clio_agent/agent.py

# 2. Run tests
uv run pytest tests/test_agent.py -v

# 3. Check code quality
uvx ruff check src/ --fix
uvx mypy src/clio_agent/

# 4. Test CLI
uv run src/clio_agent/ui/cli.py

# 5. Commit changes
git add .
git commit -m "feat: add new agent capability"
```

---

## UV Tips and Best Practices

### Performance Tips

1. **Use --frozen for CI:** Skip lock file updates in CI/CD
   ```bash
   uv sync --frozen
   ```

2. **Cache UV data:** Set `UV_CACHE_DIR` for faster installs
   ```bash
   export UV_CACHE_DIR=$HOME/.cache/uv
   ```

3. **Parallel operations:** UV automatically parallelizes downloads

4. **Use uvx for one-off tools:** Don't install globally
   ```bash
   uvx black . --check
   ```

### Dependency Management Tips

1. **Pin major versions:** Use `>=` not `==` for flexibility
   ```toml
   dependencies = ["requests>=2.31.0"]
   ```

2. **Use dependency groups:** Separate dev, test, docs dependencies

3. **Lock regularly:** Run `uv lock` after adding dependencies

4. **Check for updates:** Use `uv lock --upgrade` periodically

### Script Tips

1. **Use PEP 723:** Embed dependencies for standalone scripts

2. **Pin Python version:** Always specify `requires-python`

3. **Keep scripts focused:** One script = one task

4. **Use type hints:** Better IDE support and documentation

### Workspace Tips

1. **Shared lock file:** Single source of truth for versions

2. **Use workspace references:** `{ workspace = true }`

3. **Consistent structure:** Follow standard layout

4. **Test together:** Run tests across entire workspace

---

## Troubleshooting

### Common Issues

**Lock file out of sync:**
```bash
# Regenerate lock file
uv lock

# Or sync with --frozen to use existing lock
uv sync --frozen
```

**Python version mismatch:**
```bash
# Install correct Python version
uv python install 3.12

# Pin version
uv python pin 3.12
```

**Dependency conflicts:**
```bash
# Show resolution details
uv lock --verbose

# Try different resolution strategy
uv lock --resolution lowest-direct
```

**Cache issues:**
```bash
# Clear cache
uv cache clean

# Or specify cache directory
UV_CACHE_DIR=/tmp/uv-cache uv sync
```

### Debug Commands

```bash
# Verbose output
uv --verbose sync

# Show what would be installed
uv sync --dry-run

# Check project configuration
uv show

# Verify lock file
uv lock --check
```

---

## Quick Reference

### Essential Commands

```bash
# Project
uv init --app my-app
uv add requests
uv remove requests
uv sync

# Running
uv run python script.py
uv run --with requests python script.py

# Tools
uvx ruff check .
uv tool install black

# Python
uv python install 3.12
uv python pin 3.12

# Building
uv build
uv publish
```

### File Reference

- **pyproject.toml:** Project configuration
- **uv.lock:** Locked dependency versions
- **.python-version:** Pinned Python version
- **uv.toml:** UV-specific configuration (optional)

---

**UV is the future of Python package management. Fast, reliable, and simple.**
