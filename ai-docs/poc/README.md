# Warpio DSPy Proof of Concept

Simple DSPy-powered chat orchestrator demonstrating expert routing with a beautiful TUI.

## Features

- 🤖 **3 Expert Modules** - General Assistant, Code Helper, Data Analyst
- 🎯 **Smart Routing** - DSPy ChainOfThought automatically selects the right expert
- 💬 **Interactive Chat** - Rich TUI with history, commands, and markdown rendering
- ⚡ **UV-Powered** - Self-contained scripts with inline dependencies
- 🔧 **No MCP Yet** - Pure DSPy without external tool complexity

## Architecture

```
User Question
     │
     ▼
┌─────────────────────┐
│ WarpioOrchestrator  │  ← DSPy ChainOfThought routing
│  (orchestrator.py)  │
└─────────────────────┘
     │
     ├──→ General Assistant (general questions)
     ├──→ Code Helper (programming questions)
     └──→ Data Analyst (data/analysis questions)
```

## Prerequisites

1. **UV installed**:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **LM Studio running** (default setup):
   - Download and install [LM Studio](https://lmstudio.ai/)
   - Load model: `openai/gpt-oss-20b`
   - Start local server at `http://127.0.0.1:1234`
   - Configured parameters:
     - Temperature: 1.0
     - Top-K: 20
     - Top-P: 1.0
     - Frequency Penalty: 1.1

   **Alternative:** Use OpenAI instead (edit `config.py` to set `use_lm_studio=False`)

## Quick Start

### 1. Test Individual Experts

```bash
# Test all expert modules
uv run experts.py
```

### 2. Test Orchestrator

```bash
# Test routing with sample questions
uv run orchestrator.py
```

### 3. Launch Chat TUI

```bash
# Start interactive chat
uv run chat.py
```

## Usage

### Chat Commands

- `help` - Show help message
- `experts` - List available experts
- `history` - Show conversation history
- `clear` - Clear screen
- `exit` or `quit` - Exit chat

### Example Interactions

**General Question:**
```
You: What is machine learning?
Warpio (via general): [Detailed explanation...]
```

**Code Question:**
```
You: Write a Python function to calculate fibonacci numbers
Warpio (via code): [Explanation + code example...]
```

**Data Question:**
```
You: How should I handle missing values in a dataset?
Warpio (via data): [Analysis + recommendations...]
```

## File Structure

```
warpio_dspy_poc/
├── README.md           # This file
├── requirements.txt    # Legacy pip requirements (not needed with UV)
├── config.py          # LM Studio configuration (UV script)
├── experts.py         # Expert module definitions (UV script)
├── orchestrator.py    # Routing orchestrator (UV script)
└── chat.py           # Interactive TUI (UV script)
```

## How It Works

### 1. Signatures (Declarative Specs)

```python
class CodeHelperSignature(dspy.Signature):
    """Programming and code assistance expert."""

    question: str = dspy.InputField()
    language: str = dspy.InputField(default="python")

    explanation: str = dspy.OutputField()
    code: str = dspy.OutputField()
```

### 2. Modules (Composable Components)

```python
class CodeHelper(dspy.Module):
    def __init__(self):
        super().__init__()
        self.generate = dspy.ChainOfThought(CodeHelperSignature)

    def forward(self, question, language="python"):
        return self.generate(question=question, language=language)
```

### 3. Orchestrator (Smart Routing)

```python
class WarpioOrchestrator(dspy.Module):
    def __init__(self):
        self.router = dspy.ChainOfThought(OrchestratorSignature)
        self.experts = get_all_experts()

    def forward(self, question):
        routing = self.router(question=question, ...)
        expert = self.experts[routing.selected_expert]
        return expert(question=question)
```

## Key Benefits Over Manual Prompting

### Before (Manual Prompts)
```python
prompt = """You are a code assistant. Help users with:
- Writing code
- Debugging
- Best practices
[100+ lines of manual prompt engineering...]
"""
```

### After (DSPy)
```python
class CodeHelper(dspy.Module):
    def __init__(self):
        self.generate = dspy.ChainOfThought("question -> explanation, code")
    # Prompts auto-generated and optimizable!
```

## Future Enhancements (Full Warpio)

- [ ] Add MCP tool integration (HDF5, SLURM, ADIOS)
- [ ] Implement ReAct agents with tool calling
- [ ] Add optimization (BootstrapFewShot, MIPROv2)
- [ ] Collect usage logs for learning
- [ ] Add local AI support (Ollama, LM Studio)
- [ ] Production deployment (FastAPI, MLflow)

## Cost

Using **LM Studio (local)**:
- **$0.00 per interaction** (completely free!)
- Runs 100% locally on your hardware
- Privacy-preserving (no data sent to cloud)
- Configurable parameters for performance/quality tradeoff

**Alternative (OpenAI GPT-4o-mini)**:
- ~$0.001-0.003 per interaction
- Total: ~$0.15-0.45 per 100 interactions

## Troubleshooting

### "Connection refused" or LM Studio errors

**Check LM Studio is running:**
1. Open LM Studio application
2. Load the model `openai/gpt-oss-20b`
3. Click "Start Server" (ensure port 1234)
4. Verify at: http://127.0.0.1:1234/v1/models

**Verify configuration:**
```bash
# Test LM Studio connection
uv run config.py
```

### "uv: command not found"
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc  # or ~/.zshrc
```

### Want to use OpenAI instead?

Edit `config.py` and change the main functions:
```python
# In chat.py, orchestrator.py, experts.py
lm = setup_dspy(use_lm_studio=False)  # Use OpenAI
```

Then set your API key:
```bash
export OPENAI_API_KEY='sk-...'
```

### Import errors
All dependencies are inline - UV handles them automatically. Just run:
```bash
uv run chat.py
```

## Next Steps

1. **Test the POC**: Run through the quick start
2. **Collect Examples**: Save good interactions for optimization
3. **Add MCP Tools**: Integrate scientific computing tools
4. **Optimize**: Run BootstrapFewShot to improve prompts
5. **Deploy**: Package as full Warpio replacement

## Resources

- [DSPy Documentation](https://dspy.ai)
- [UV Documentation](https://docs.astral.sh/uv/)
- [Warpio DSPy Research](../docs/DSPY_FOR_WARPIO.md)

---

**Version:** 0.1.0 POC
**Status:** ✅ Working (no MCP yet)
**Next:** Add MCP tool integration and optimization
