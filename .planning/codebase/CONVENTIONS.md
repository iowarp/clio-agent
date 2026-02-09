# Coding Conventions

**Analysis Date:** 2026-02-09

## Naming Patterns

**Files:**
- Modules: lowercase with underscores (`data_expert.py`, `arc_memory.py`)
- Classes in modules: single-file modules have class names in PascalCase (`LSMTree`, `ARCMemory`, `DataExpert`)
- Test files: `test_*.py` prefix for unit tests, organized in `tests/` directories by module

**Functions:**
- Lowercase with underscores: `fetch_lm_studio_models()`, `configure_dspy_lm_studio()`, `setup_dspy()`
- Private/internal functions: prefix with single underscore `_current_file`, `_src_root`
- Tool/utility functions: descriptive names matching their action: `ask_data_expert()`, `hdf5_analyze()`, `hdf5_optimize()`
- Getters/setters: use `get_*` prefix explicitly: `get_cache_stats()`, `get_capabilities()`, `get_agents()`

**Variables:**
- Local variables: lowercase with underscores (`temp_path`, `metric`, `main_model`)
- Constants: UPPERCASE with underscores (none found yet, but follow Python convention)
- Private/module-level: prefix with underscore (`_loop_exception_handler`, `_lock`, `_conv_dir`, `_cache`)
- Class attributes: public without prefix (e.g., `self.data_dir`), private with underscore prefix (e.g., `self._cache`)
- Type aliases in signatures: lowercase descriptive names (`session_context`, `file_context`)

**Types/Classes:**
- DSPy Signature classes: `{Domain}{Purpose}Signature` (e.g., `MainAgentSignature`, `DataExpertSignature`)
- Dataclasses: `{Name}Config` for configuration classes (e.g., `LMStudioConfig`, `RouterLMConfig`, `ReasonerLMConfig`)
- msgspec Struct classes: same pattern as dataclasses, used for schema objects (e.g., `Message`, `Conversation`, `Invocation`)
- Custom exceptions: `{Description}Error` or `{Description}Exception` (e.g., `MCPError`, `MCPServerUnavailable`, `MCPToolNotFound`)

## Code Style

**Formatting:**
- Line length: 100 characters (configured in `pyproject.toml`)
- Target version: Python 3.12+ (configured in `pyproject.toml`)
- Tool: Black-compatible formatting (ruff used for linting)

**Linting:**
- Tool: Ruff (`pyproject.toml`, line 124-137)
- Enabled rules: E (errors), W (warnings), F (pyflakes), I (isort), B (bugbear), C4 (comprehensions)
- Ignored: E501 (line length, handled by Black)
- Configuration: `line-length = 100`, `target-version = "py312"`

**Formatting Details:**
- Use standard Python dataclasses and msgspec.Struct for data containers
- Use double quotes for strings (Python style)
- Imports organized with isort (configured via ruff)
- No semicolons at end of lines
- Space before colons in dicts/slices: `[1 : 3]` not `[1:3]`

## Import Organization

**Order:**
1. Standard library imports (sys, time, threading, pathlib, etc.)
2. Third-party imports (dspy, requests, msgspec, rich)
3. Local/relative imports from clio_agent package

**Path Aliases:**
- Use absolute imports within package: `from clio_agent.arc.memory import ARCMemory`
- Use relative imports for same-package dependencies: generally avoided, use absolute
- Special handling: UV script files add src/ to sys.path manually (`_current_file`, `_src_root` pattern in `agent.py`, `config.py`, etc.)

**Example from `agent.py` (lines 59-63):**
```python
# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent  # src/clio_agent/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))
```

## Error Handling

**Patterns:**
- Use specific exception types, not generic `Exception` (e.g., `MCPServerUnavailable` instead of `Exception`)
- Custom exceptions inherit from base exception class (e.g., `MCPError` base for all MCP exceptions)
- Exceptions defined in their respective modules (`src/clio_agent/tools/mcp_wrapper.py` lines 95-105)

**Example (lines 173-187 in `mcp_wrapper.py`):**
```python
try:
    # Operation
except ConnectionError as e:
    raise MCPServerUnavailable(
        server=server,
        f"Ensure server is running. Error: {e}"
    )
except Exception as e:
    if tool_name not in str(e):
        raise MCPToolNotFound(f"Tool '{tool_name}' not found on server '{server}'")
    raise MCPError(f"MCP call failed: {e}")
```

**Error Messages:**
- Include context (what failed, why, how to fix)
- Use emoji prefixes in console output: `✓` for success, `❌` for errors, `⏳` for waiting
- Example from `config.py` (lines 66-68):
```python
print(f"❌ Could not connect to LM Studio after {max_retries} attempts")
print(f"   Please ensure LM Studio is running at {base_url}")
```

## Logging

**Framework:** No centralized logging framework. Uses print() for console output.

**Patterns:**
- Print status messages to stdout with emoji prefixes
- Print error details to stdout (not stderr)
- Verbose mode controlled by function parameter: `verbose: bool = True`
- Example from `config.py` (lines 269-273):
```python
if verbose:
    print(f"✓ LM Studio configured")
    print(f"  URL: {config.base_url}")
    print(f"  Model: {config.model}")
```

## Comments

**When to Comment:**
- Comment complex algorithms or non-obvious logic
- Use module-level docstrings to explain purpose and usage
- Avoid comments for obvious code
- Use `# TODO:` and `# FIXME:` for incomplete work
- Use horizontal dividers (`# ============...`) to section large files

**Example section marker from `config.py` (line 29-30):**
```python
# ============================================================================
# LM STUDIO MODEL FETCHING
# ============================================================================
```

**JSDoc/TSDoc:**
- Use Google-style docstrings for all functions and classes
- Required for all functions: description, Args, Returns sections
- Optional: Example, Note, Raises sections

**Example from `config.py` (lines 33-42):**
```python
def fetch_lm_studio_models(base_url: str = "http://127.0.0.1:1234", max_retries: int = 10, retry_delay: float = 2.0) -> List[str]:
    """Fetch available models from LM Studio API with retry logic.

    Args:
        base_url: LM Studio base URL
        max_retries: Maximum connection attempts
        retry_delay: Delay between retries in seconds

    Returns:
        List of model IDs
    """
```

## Function Design

**Size:** Functions should be focused and readable. Target: 30-50 lines maximum.
- Complex operations should be broken into smaller functions
- Each function has single responsibility

**Parameters:**
- Prefer explicit parameters over *args/**kwargs
- Use type hints on all parameters
- Default values for optional parameters
- Example from `config.py` (lines 241-243):
```python
def setup_dspy(
    model: Optional[str] = None,
    verbose: bool = True
) -> dspy.LM:
```

**Return Values:**
- Always include return type annotation
- Return structured data (dicts, dataclasses, Struct objects) not tuples for multiple values
- Example from `config.py` (lines 72-75):
```python
def select_models_for_agents(models: List[str]) -> tuple[str, str]:
    """..."""
    # Returns tuple of (main_model, expert_model)
```

## Module Design

**Exports:**
- Use `__all__` when module has multiple public classes/functions
- Modules typically export single main class (e.g., `ARCMemory`, `DataExpert`, `ClioAgent`)
- Internal helper functions prefixed with underscore

**Barrel Files:**
- Used in `__init__.py` files to re-export public APIs
- Example from `src/clio_agent/arc/__init__.py`:
```python
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Conversation, Invocation, Metrics
```

**File Structure Pattern:**
- Module file layout: docstring → imports → constants/section markers → main class/function → utility functions → `if __name__ == "__main__":` test block
- Large modules: use horizontal comment dividers to separate sections
- Example from `config.py`: Sections for fetching (lines 29-69), configuration classes (lines 129-166), setup functions (lines 170-287), test main (lines 289-315)

## Type Hints

**Required:**
- All function parameters: `def setup_dspy(model: Optional[str] = None, verbose: bool = True) -> dspy.LM:`
- All return types: `-> tuple[str, str]:`, `-> List[str]:`, `-> Dict[str, Any]:`
- Supported types: `Optional[T]`, `List[T]`, `Dict[K, V]`, `Any`, `Literal["value"]`

**MyPy Configuration:**
- Configured in `pyproject.toml` (lines 139-144)
- `disallow_untyped_defs = false` (not strict yet)
- `warn_return_any = true` (warns on untyped returns)
- `ignore_missing_imports = true` (allows third-party without stubs)

## Dataclass/Struct Patterns

**msgspec.Struct (preferred for schema):**
- Used for serializable data structures (ARC memory schemas)
- All fields have type hints
- Include docstrings for each field
- Example from `schema.py` (lines 28-54):
```python
class Message(msgspec.Struct):
    """Individual message within a conversation."""
    role: str  # "user" or "assistant"
    content: str
    timestamp: float  # Unix timestamp from time.time()
    message_id: str = msgspec.field(default_factory=lambda: str(uuid.uuid4()))
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)
```

**Python dataclass:**
- Used for configuration objects
- Example from `config.py` (lines 135-145):
```python
@dataclass
class LMStudioConfig:
    """Configuration for LM Studio provider."""
    base_url: str = "http://127.0.0.1:1234"
    model: str = "ibm/granite-4-h-tiny"
    temperature: float = 1.0
    max_tokens: int = 32000
    api_key: str = "lm-studio"
```

## DSPy-Specific Patterns

**Signatures:**
- Inherit from `dspy.Signature`
- Use comprehensive docstrings (500+ words for domain experts) as system prompt
- All fields have type hints and descriptions
- Example from `expert_sig.py` (lines 35-60):
```python
class DataExpertSignature(dspy.Signature):
    """You are the CLIO Data Expert..."""
    question: str = dspy.InputField()
    file_context: str = dspy.InputField()
    analysis: str = dspy.OutputField()
    recommendations: str = dspy.OutputField()
```

**Modules (ReAct/ChainOfThought):**
- Create instances with signature and tools
- Store module-level instances for global reuse (antipattern but used: see `agent.py` line 122)
- Use `dspy.context(lm=model)` for per-request model selection instead of global `dspy.configure()`

---

*Convention analysis: 2026-02-09*
