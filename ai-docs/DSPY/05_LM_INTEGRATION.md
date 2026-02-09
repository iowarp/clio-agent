# DSPy 3.x LM Integration Guide
> Version: dspy-ai 3.1.3 | Updated: February 2026

Complete guide to language model integration in DSPy, covering cloud providers, local models, configuration, adapters, and cost optimization.

---

## Overview

DSPy provides a unified `dspy.LM()` interface for 50+ language model providers via LiteLLM. Write model-agnostic code once, then switch between OpenAI, Anthropic, local Ollama, and more without changing your program.

**Key Benefits**:
- **Unified API**: Same interface for all providers
- **Model-agnostic code**: Switch models without rewriting
- **Local AI support**: Ollama, LM Studio, SGLang integration
- **Built-in features**: Caching, retries, usage tracking, streaming
- **Thread-safe**: Safe configuration in concurrent environments

---

## dspy.LM() Full Constructor

```python
import dspy

lm = dspy.LM(
    model='provider/model-name',        # Required: Model identifier
    api_key=None,                       # API key (or env: PROVIDER_API_KEY)
    api_base=None,                      # Custom API endpoint
    temperature=None,                   # Randomness (0.0-2.0)
    max_tokens=None,                    # Output length limit
    top_p=None,                         # Nucleus sampling
    frequency_penalty=None,             # Repetition penalty
    presence_penalty=None,              # Topic diversity
    stop=None,                          # Stop sequences (list)
    logprobs=None,                      # Return log probabilities
    response_format=None,               # JSON schema for structured output
    cache=True,                         # Enable caching
    num_retries=3,                      # Retry on failures
    timeout=None,                       # Request timeout (seconds)
    model_type='chat',                  # 'chat' or 'text'
    # Advanced
    callbacks=None,                     # List of callback handlers
    rollout_id=None,                    # Unique ID for non-deterministic runs
    track_usage=True,                   # Track token usage
    # Provider-specific (passed through)
    **kwargs                            # Additional provider params
)
```

### Core Parameters

**model** (required):
- Format: `'provider/model-name'`
- Examples: `'openai/gpt-4o'`, `'anthropic/claude-opus-4-6'`, `'ollama_chat/llama3.2'`

**api_key**:
- API key for authentication
- If None, reads from environment: `{PROVIDER}_API_KEY`
- For local models: use empty string `''`

**api_base**:
- Custom API endpoint
- For local models: `'http://localhost:11434'` (Ollama), `'http://localhost:1234/v1'` (LM Studio)
- For enterprise: Azure, AWS endpoints

**temperature**:
- Controls randomness (0.0 = deterministic, 2.0 = very creative)
- Default varies by model (usually 0.7-1.0)
- For factual tasks: 0.0-0.3
- For creative tasks: 0.7-1.5

**max_tokens**:
- Maximum output length
- Default: model-specific (usually 2048-4096)
- Set based on task needs

---

## Provider Format and Configuration

### OpenAI

```python
import os
import dspy

# Set API key
os.environ['OPENAI_API_KEY'] = 'sk-...'

# Standard models
lm = dspy.LM('openai/gpt-4o')
lm = dspy.LM('openai/gpt-4o-mini')
lm = dspy.LM('openai/gpt-3.5-turbo')

# Reasoning models (special requirements)
lm = dspy.LM(
    'openai/o1-preview',
    temperature=1.0,        # Must be 1.0 or None
    max_tokens=16000        # Must be >= 16000 or None
)

dspy.configure(lm=lm)
```

**Features**:
- Native function calling
- Prompt caching (GPT-4o, GPT-4o-mini)
- Vision support (GPT-4o)
- JSON mode with `response_format`

### Anthropic (Claude)

```python
import os
import dspy

os.environ['ANTHROPIC_API_KEY'] = 'sk-ant-...'

# Claude models
lm = dspy.LM('anthropic/claude-opus-4-6')
lm = dspy.LM('anthropic/claude-3-5-sonnet-20241022')
lm = dspy.LM('anthropic/claude-3-haiku-20240307')

dspy.configure(lm=lm)
```

**Features**:
- Large context windows (200K+ tokens)
- Native tool use
- Thinking/reasoning support
- Prompt caching (beta)

### Google Gemini

```python
import os
import dspy

os.environ['GEMINI_API_KEY'] = '...'

lm = dspy.LM('gemini/gemini-2.5-pro-preview-03-25')
lm = dspy.LM('gemini/gemini-2.5-flash')

dspy.configure(lm=lm)
```

**Features**:
- Multi-modal (text, images, audio, video)
- Long context (up to 2M tokens)
- Competitive pricing

### Databricks

```python
import dspy

# Automatic auth when running on Databricks
lm = dspy.LM('databricks/llama-70b')

# Or set manually
import os
os.environ['DATABRICKS_API_KEY'] = '...'
os.environ['DATABRICKS_API_BASE'] = 'https://your-workspace.databricks.com'

dspy.configure(lm=lm)
```

### Additional Cloud Providers

All via LiteLLM with format `'provider/model-name'`:

```python
# Azure OpenAI
lm = dspy.LM('azure/gpt-4-deployment-name')

# AWS Bedrock
lm = dspy.LM('bedrock/anthropic.claude-v2')

# AWS SageMaker
lm = dspy.LM('sagemaker/endpoint-name')

# Vertex AI
lm = dspy.LM('vertex_ai/gemini-pro')

# Together AI
lm = dspy.LM('together_ai/meta-llama/Llama-3-70b-chat-hf')

# Anyscale
lm = dspy.LM('anyscale/mistralai/Mistral-7B-Instruct-v0.1')

# Fireworks AI
lm = dspy.LM('fireworks_ai/llama-v3-70b-instruct')

# And many more...
```

---

## Local Model Integration

### Ollama (Recommended)

**Installation**:
```bash
# Linux/macOS
curl -fsSL https://ollama.ai/install.sh | sh

# Pull models
ollama pull llama3.2:1b
ollama pull llama3.1:8b
ollama pull mistral:7b
```

**DSPy Configuration (Method 1 - Modern)**:
```python
import dspy

lm = dspy.LM(
    'ollama_chat/llama3.2',
    api_base='http://localhost:11434',
    api_key='',                         # Empty for local
    temperature=0.3,
    max_tokens=2048
)

dspy.configure(lm=lm)
```

**DSPy Configuration (Method 2 - OllamaLocal Class)**:
```python
import dspy

lm = dspy.OllamaLocal(
    model='llama3.1:8b-instruct-q5_1',
    max_tokens=4000,
    timeout_s=480                       # 8 minutes for larger models
)

dspy.configure(lm=lm)
```

**Available Models** (via `ollama list`):
- Llama 3.2: 1B, 3B (fast, efficient)
- Llama 3.1: 8B, 70B, 405B (balanced, high quality)
- Mistral: 7B, 8x7B (excellent performance)
- Gemma: 2B, 7B (Google, efficient)
- Phi-3: 3B, 14B (Microsoft, strong reasoning)
- CodeLlama: 7B, 13B, 34B (code generation)
- DeepSeek: 6.7B (code understanding)

**Quantization Options**:
- `q4_K_M`: 4-bit, medium quality, fast
- `q5_1`: 5-bit, better quality
- `q8_0`: 8-bit, near-full quality

### LM Studio

**Setup**:
1. Download LM Studio: https://lmstudio.ai/
2. Browse and download models from GUI
3. Load model and start server (default port: 1234)

**DSPy Configuration**:
```python
import dspy

lm = dspy.LM(
    'openai/llama-3.2-3b-instruct',     # Model name from LM Studio
    api_base='http://localhost:1234/v1',
    api_key='lm-studio',                # Any value works
    model_type='chat',
    temperature=0.3,
    max_tokens=2000
)

dspy.configure(lm=lm)
```

**Advantages**:
- User-friendly GUI
- Built-in model browser
- Performance monitoring
- Easy model switching

### SGLang (GPU-Optimized)

**Installation**:
```bash
pip install "sglang[all]"

# Launch server
python -m sglang.launch_server \
    --model-path meta-llama/Llama-3.1-8B-Instruct \
    --port 30000
```

**DSPy Configuration**:
```python
import dspy

lm = dspy.LM(
    'openai/llama-3.1-8b-instruct',
    api_base='http://localhost:30000/v1',
    api_key='EMPTY',
    temperature=0.3,
    max_tokens=4096
)

dspy.configure(lm=lm)
```

**Use Cases**:
- High-throughput inference
- Production local deployments
- Fine-tuned model serving
- GPU cluster deployments

### Other OpenAI-Compatible Servers

DSPy works with any OpenAI-compatible API:

```python
# Generic pattern
lm = dspy.LM(
    'openai/model-name',
    api_base='http://your-server:port/v1',
    api_key='your-key-or-empty'
)
```

Compatible servers: vLLM, TGI, LocalAI, FastChat, llama.cpp, KoboldAI

---

## Configuration Patterns

### Global Configuration

Set default LM for all modules:

```python
import dspy

lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm)

# All modules use this LM
qa = dspy.ChainOfThought('question -> answer')
result = qa(question="What is DSPy?")
```

### Context Manager (Scoped Configuration)

Temporarily switch LMs:

```python
import dspy

# Global default
dspy.configure(lm=dspy.LM('openai/gpt-3.5-turbo'))

# Temporarily use different model
with dspy.context(lm=dspy.LM('openai/gpt-4o')):
    result = complex_module(query)
    # Uses GPT-4o

# Back to global default
result = simple_module(query)
# Uses GPT-3.5-turbo
```

**Benefits**:
- Thread-safe context switching
- Per-request model selection
- Cost optimization (cheap for simple, expensive for complex)

### Module-Level Configuration

Set LM for specific module:

```python
import dspy

class MyModule(dspy.Module):
    def __init__(self):
        self.predictor = dspy.ChainOfThought('input -> output')

        # Set specific LM for this module
        self.predictor.set_lm(dspy.LM('openai/gpt-4o'))

    def forward(self, input):
        return self.predictor(input=input)

# Other modules use global LM
# This module uses GPT-4o
```

### Multiple LMs in One Program

```python
import dspy

class MultiModelPipeline(dspy.Module):
    def __init__(self):
        # Different LMs for different stages
        self.cheap_lm = dspy.LM('openai/gpt-3.5-turbo')
        self.expensive_lm = dspy.LM('openai/gpt-4o')

        self.query_rewriter = dspy.Predict('question -> rewritten_query')
        self.answer_generator = dspy.ChainOfThought('question -> answer')

    def forward(self, question):
        # Stage 1: Cheap rewrite
        with dspy.context(lm=self.cheap_lm):
            rewritten = self.query_rewriter(question=question)

        # Stage 2: Expensive generation
        with dspy.context(lm=self.expensive_lm):
            answer = self.answer_generator(question=rewritten.rewritten_query)

        return answer
```

---

## Adapter System

Adapters control how signatures are formatted into prompts and how responses are parsed.

### ChatAdapter (Default)

Field-based formatting with delimiters.

```python
import dspy

adapter = dspy.ChatAdapter()
dspy.configure(adapter=adapter)

# Or set per-LM
lm = dspy.LM('openai/gpt-4o-mini', adapter=adapter)
```

**How it works**:
```
System: Follow the instructions.

---

Question: What is 2+2?

---

Answer: [to be generated]
```

**When to use**:
- Universal compatibility (works with all models)
- Default choice
- Reliable parsing

### JSONAdapter

Native JSON generation using `response_format`.

```python
import dspy

adapter = dspy.JSONAdapter()
dspy.configure(adapter=adapter)

# Requires model support (OpenAI, Anthropic latest)
lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm, adapter=adapter)
```

**Benefits**:
- Lower latency (no parsing)
- Cleaner output
- Guaranteed valid JSON

**Requirements**:
- Model must support `response_format` parameter
- OpenAI: GPT-4o, GPT-4o-mini, GPT-3.5-turbo (latest)
- Anthropic: Claude 3.5+ (with tool use mode)

### XMLAdapter

XML-based formatting (for models trained on XML).

```python
import dspy

adapter = dspy.XMLAdapter()
dspy.configure(adapter=adapter)
```

**When to use**:
- Models fine-tuned on XML (some Claude versions prefer this)
- Hierarchical data structures

### TwoStepAdapter

Separate reasoning and extraction steps.

```python
import dspy

class CustomTwoStepAdapter(dspy.TwoStepAdapter):
    def __init__(self):
        # Reasoning model
        self.reasoning_lm = dspy.LM('openai/gpt-4o')
        # Extraction model
        self.extraction_lm = dspy.LM('openai/gpt-3.5-turbo')
        super().__init__(
            reasoning_lm=self.reasoning_lm,
            extraction_lm=self.extraction_lm
        )

adapter = CustomTwoStepAdapter()
dspy.configure(adapter=adapter)
```

**When to use**:
- Complex parsing needs
- Separate reasoning from formatting
- Cost optimization (cheap extraction)

### Adapter Flow

```
Signature
    ↓
Adapter.format()
    ↓
Formatted messages/prompt
    ↓
LM call
    ↓
Raw response
    ↓
Adapter.parse()
    ↓
Structured output (dspy.Prediction)
```

---

## dspy.Embedder

For embeddings (used in retrieval, KNNFewShot, etc).

```python
import dspy

# Hosted embeddings
embedder = dspy.Embedder(
    model='openai/text-embedding-3-small',
    api_key=None,  # From environment
    dimensions=None  # Use model default
)

# Local embeddings (via sentence-transformers)
from sentence_transformers import SentenceTransformer

class LocalEmbedder:
    def __init__(self, model_name='all-MiniLM-L6-v2'):
        self.model = SentenceTransformer(model_name)

    def __call__(self, texts):
        return self.model.encode(texts)

embedder = LocalEmbedder()

# Use in retrieval
retriever = dspy.Retrieve(embedder=embedder, k=5)
```

---

## Usage Tracking

Track token usage and costs.

```python
import dspy

# Enable globally
dspy.configure_usage_tracking(enable=True)

# Make predictions
lm = dspy.LM('openai/gpt-4o-mini', track_usage=True)
dspy.configure(lm=lm)

qa = dspy.ChainOfThought('question -> answer')
result = qa(question="What is DSPy?")

# Get usage
usage = result.get_lm_usage()
print(f"Prompt tokens: {usage['prompt_tokens']}")
print(f"Completion tokens: {usage['completion_tokens']}")
print(f"Total tokens: {usage['total_tokens']}")
print(f"Estimated cost: ${usage['cost']:.4f}")

# Or access LM history
print(lm.history)  # List of all calls with usage
```

**Cost Tracking Example**:
```python
class CostTracker:
    def __init__(self):
        self.total_cost = 0
        self.calls = []

    def track_call(self, usage):
        cost = self.calculate_cost(usage)
        self.total_cost += cost
        self.calls.append({'usage': usage, 'cost': cost})

    def calculate_cost(self, usage):
        # Price per 1M tokens (update as needed)
        prices = {
            'gpt-4o': {'input': 2.50, 'output': 10.00},
            'gpt-4o-mini': {'input': 0.15, 'output': 0.60},
        }
        model = usage.get('model', 'gpt-4o-mini')
        input_cost = (usage['prompt_tokens'] / 1_000_000) * prices[model]['input']
        output_cost = (usage['completion_tokens'] / 1_000_000) * prices[model]['output']
        return input_cost + output_cost

tracker = CostTracker()

# After each call
usage = result.get_lm_usage()
tracker.track_call(usage)

print(f"Total spent: ${tracker.total_cost:.2f}")
```

---

## Caching

DSPy implements three-tier caching.

### Tier 1: In-Memory Cache

- Technology: `cachetools.LRUCache`
- Scope: Current process
- Speed: Fastest (~1ms)

### Tier 2: Disk Cache

- Technology: `diskcache.FanoutCache`
- Scope: Persistent across runs
- Speed: Fast (~5-10ms)

### Tier 3: Provider Cache

- Technology: Provider-specific (e.g., OpenAI prompt caching)
- Scope: Provider-managed
- Speed: Medium (~50-100ms, but cheaper)

### Cache Configuration

```python
import dspy
import os

# Set cache directory
os.environ['DSP_CACHEDIR'] = '/path/to/cache'

# Configure cache
dspy.configure_cache(
    enable_disk_cache=True,
    enable_memory_cache=True,
    disk_size_limit_bytes=10 * 1024 * 1024 * 1024,  # 10GB
    memory_max_entries=10000
)

# Disable caching for specific LM
lm = dspy.LM('openai/gpt-4o-mini', cache=False)
```

### Cache Bypass (Non-Deterministic Sampling)

```python
import uuid
import dspy

# Each call gets unique rollout ID (bypasses cache)
lm = dspy.LM('openai/gpt-4o-mini', rollout_id=str(uuid.uuid4()))

# Or copy LM with new rollout ID
lm_fresh = lm.copy(rollout_id=str(uuid.uuid4()))
```

### Clear Cache

```python
import shutil
import os

cache_dir = os.environ.get('DSP_CACHEDIR', os.path.expanduser('~/.dspy_cache'))
shutil.rmtree(cache_dir)
os.makedirs(cache_dir)
```

---

## Error Handling and Retries

### Built-in Retry Logic

DSPy automatically retries on transient errors:

```python
import dspy

lm = dspy.LM(
    'openai/gpt-4o-mini',
    num_retries=5  # Default: 3
)
```

**Retry Triggers**:
- Network errors
- Rate limit errors (429)
- Server errors (500, 502, 503)
- Timeout errors

**Backoff Strategy**:
- Exponential: 1s, 2s, 4s, 8s, 16s
- Max delay: 60s

### Custom Error Handling

```python
import dspy
import time
from openai import RateLimitError, APIError

class ResilientLM:
    def __init__(self, lm, max_retries=5):
        self.lm = lm
        self.max_retries = max_retries

    def __call__(self, *args, **kwargs):
        for attempt in range(self.max_retries):
            try:
                return self.lm(*args, **kwargs)

            except RateLimitError:
                wait = min(2 ** attempt, 60)
                print(f"Rate limited, waiting {wait}s...")
                time.sleep(wait)

            except APIError as e:
                if e.status_code >= 500:
                    print(f"Server error, retrying...")
                    time.sleep(2 ** attempt)
                else:
                    raise  # Client error, don't retry

        raise Exception(f"Failed after {self.max_retries} retries")

# Use
lm = dspy.LM('openai/gpt-4o-mini')
resilient_lm = ResilientLM(lm)
dspy.configure(lm=resilient_lm)
```

---

## Multi-Model Patterns

### Teacher-Student Optimization

```python
import dspy
from dspy.teleprompt import BootstrapFewShot

# Teacher (strong, expensive)
teacher_lm = dspy.LM('openai/gpt-4o')

# Student (weak, cheap)
student_lm = dspy.LM('openai/gpt-3.5-turbo')

# Configure student as default
dspy.configure(lm=student_lm)

# Optimize with teacher
optimizer = BootstrapFewShot(
    metric=metric,
    teacher_settings={'lm': teacher_lm}  # Teacher generates demos
)

compiled = optimizer.compile(student=module, trainset=trainset)

# Student now performs much better at lower cost
```

### Dynamic Model Selection

```python
import dspy

def select_model(complexity, budget):
    if budget < 0.001:
        return dspy.LM('ollama_chat/llama3.2', api_base='http://localhost:11434', api_key='')
    elif complexity == "simple":
        return dspy.LM('openai/gpt-3.5-turbo')
    elif complexity == "medium":
        return dspy.LM('openai/gpt-4o-mini')
    else:
        return dspy.LM('anthropic/claude-opus-4-6')

# Use
complexity = analyze_query_complexity(query)
budget = get_user_budget()
lm = select_model(complexity, budget)

with dspy.context(lm=lm):
    result = module(query=query)
```

### Fallback Chain

```python
import dspy

class FallbackLM:
    def __init__(self, primary, fallbacks):
        self.primary = primary
        self.fallbacks = fallbacks

    def __call__(self, *args, **kwargs):
        lms = [self.primary] + self.fallbacks

        for i, lm in enumerate(lms):
            try:
                result = lm(*args, **kwargs)
                if i > 0:
                    print(f"Used fallback {i}")
                return result
            except Exception as e:
                if i == len(lms) - 1:
                    raise e
                print(f"LM {i} failed, trying fallback...")
                continue

# Setup
primary = dspy.LM('openai/gpt-4o')
fallbacks = [
    dspy.LM('openai/gpt-4o-mini'),
    dspy.LM('openai/gpt-3.5-turbo'),
    dspy.LM('ollama_chat/llama3.2', api_base='http://localhost:11434', api_key='')
]

lm = FallbackLM(primary, fallbacks)
dspy.configure(lm=lm)
```

---

## CLIO Agent LM Configuration

### Recommended Setup

```python
import dspy
import os

# Main orchestrator (lightweight, cloud)
orchestrator_lm = dspy.LM(
    'openai/gpt-4o-mini',
    temperature=0.3,
    max_tokens=1000
)

# Data Expert (privacy-sensitive, local)
data_expert_lm = dspy.LM(
    'ollama_chat/llama3.1:8b',
    api_base='http://localhost:11434',
    api_key='',
    temperature=0.2,
    max_tokens=4096,
    timeout_s=300
)

# Analysis Expert (complex reasoning, cloud or local 70B)
analysis_expert_lm = dspy.LM(
    'openai/gpt-4o',
    temperature=0.3,
    max_tokens=4096
)

# Configure globally (orchestrator)
dspy.configure(lm=orchestrator_lm)

# Experts use context manager
class DataExpert(dspy.Module):
    def __init__(self):
        self.lm = data_expert_lm
        self.analyzer = dspy.ChainOfThought('task -> analysis, recommendations')

    def forward(self, task):
        with dspy.context(lm=self.lm):
            return self.analyzer(task=task)
```

### Configuration File

```python
# config/models.json
{
    "orchestrator": {
        "model": "openai/gpt-4o-mini",
        "temperature": 0.3,
        "max_tokens": 1000
    },
    "experts": {
        "data_expert": {
            "model": "ollama_chat/llama3.1:8b",
            "api_base": "http://localhost:11434",
            "api_key": "",
            "temperature": 0.2,
            "max_tokens": 4096
        },
        "analysis_expert": {
            "model": "openai/gpt-4o",
            "temperature": 0.3,
            "max_tokens": 4096
        }
    }
}
```

```python
import json
import dspy

def load_lm_config(config_path='config/models.json'):
    with open(config_path) as f:
        config = json.load(f)

    lms = {}

    # Orchestrator
    orch_cfg = config['orchestrator']
    lms['orchestrator'] = dspy.LM(
        orch_cfg['model'],
        temperature=orch_cfg['temperature'],
        max_tokens=orch_cfg['max_tokens']
    )

    # Experts
    for name, cfg in config['experts'].items():
        lms[name] = dspy.LM(
            cfg['model'],
            api_base=cfg.get('api_base'),
            api_key=cfg.get('api_key', None),
            temperature=cfg['temperature'],
            max_tokens=cfg['max_tokens']
        )

    return lms

# Use
lms = load_lm_config()
dspy.configure(lm=lms['orchestrator'])

# Experts use their specific LMs
with dspy.context(lm=lms['data_expert']):
    result = data_expert(task)
```

---

## Best Practices

### 1. Development
- Start with cloud models (GPT-4o-mini) for rapid prototyping
- Use caching extensively to save costs
- Enable usage tracking from day one

### 2. Optimization
- Use teacher-student (cloud teacher → local student)
- Optimize with cheap models (GPT-4o-mini)
- Deploy to expensive or local models

### 3. Production
- Use local models for sensitive data
- Implement fallback chains for reliability
- Monitor costs and usage continuously
- Enable caching for repeated queries
- Version lock models for reproducibility

### 4. Local AI
- Use Ollama for ease of deployment
- Quantize models (Q4_K_M or Q5_1) for speed
- Monitor memory usage
- Set appropriate timeouts (300-600s for large models)
- Batch processing to amortize startup costs

### 5. Cost Optimization
- Route simple queries to cheap models
- Route complex queries to expensive models
- Use caching aggressively
- Implement budget limits
- Track costs per query/user/session

---

## Troubleshooting

### "LM not responding"
```python
# Test LM directly
lm = dspy.LM('openai/gpt-4o-mini')
try:
    result = lm("Test message")
    print(f"Success: {result}")
except Exception as e:
    print(f"Error: {e}")
    # Check: API key, network, rate limits, model availability
```

### "Ollama connection refused"
```bash
# Check Ollama status
curl http://localhost:11434/api/generate -d '{
  "model": "llama3.2:3b",
  "prompt": "test",
  "stream": false
}'

# Restart if needed
pkill ollama && ollama serve
```

### "Slow local inference"
```python
# Use quantized model
lm = dspy.LM(
    'ollama_chat/llama3.1:8b-q4_K_M',  # Quantized
    api_base='http://localhost:11434',
    api_key='',
    max_tokens=1024  # Reduce if needed
)
```

### "Rate limit errors"
```python
# Increase retries
lm = dspy.LM('openai/gpt-4o-mini', num_retries=10)

# Or implement exponential backoff (see Error Handling section)
```

---

## Summary

DSPy's LM integration provides:

1. **Unified interface**: One API for 50+ providers
2. **Model-agnostic code**: Switch models without rewriting
3. **Local AI support**: Ollama, LM Studio, SGLang
4. **Built-in features**: Caching, retries, usage tracking, streaming
5. **Flexible configuration**: Global, scoped, module-level
6. **Adapter system**: Control prompt formatting
7. **Multi-model patterns**: Teacher-student, fallbacks, dynamic selection

**For CLIO Agent**:
- Orchestrator: GPT-4o-mini (cloud, lightweight)
- Data Expert: Llama-3.1-8B (local, privacy)
- Analysis Expert: GPT-4o (cloud, complex reasoning) or Llama-3.1-70B (local)
- Hybrid approach: Optimize with cloud, deploy to local

Next: [Advanced Patterns](./06_ADVANCED_PATTERNS.md) for async, streaming, assertions, and more.
