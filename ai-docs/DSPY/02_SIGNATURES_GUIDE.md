# DSPy 3.x Signatures Guide
> Version: dspy-ai 3.1.3 | Updated: February 2026

## Table of Contents

1. [Overview](#overview)
2. [Inline Signatures](#inline-signatures)
3. [Class-Based Signatures](#class-based-signatures)
4. [Input and Output Fields](#input-and-output-fields)
5. [Typed Outputs](#typed-outputs)
6. [Signature Manipulation](#signature-manipulation)
7. [Serialization](#serialization)
8. [Multi-Modal Signatures](#multi-modal-signatures)
9. [Signature Design Guidelines](#signature-design-guidelines)
10. [CLIO Agent Usage](#clio-agent-usage)

---

## Overview

Signatures in DSPy 3.x define the input-output contract for LM modules. They specify what fields the model receives and what fields it should produce. Signatures support both string-based (inline) and class-based definitions with full type safety.

**Key Features:**
- Inline string syntax for quick prototyping
- Class-based signatures with Pydantic integration
- Typed outputs (primitives, Literal, lists, dicts, Pydantic models)
- Immutable manipulation methods
- Multi-modal support (images, audio)
- Serialization for caching and persistence

---

## Inline Signatures

Inline signatures use string syntax with arrow notation to define input-output contracts.

### Basic Syntax

```python
import dspy

# Simple transformation
"question -> answer"

# Typed outputs
"sentence -> sentiment: bool"
"document -> summary"

# Multiple inputs
"context: list[str], question: str -> answer: str"

# Multiple outputs
"question, choices: list[str] -> reasoning: str, selection: int"
```

### With Instructions

Add instructions to guide the model's behavior:

```python
# Create signature with instructions
signature = dspy.Signature(
    "comment -> toxic: bool",
    instructions="Mark as 'toxic' if comment includes insults, harassment, or sarcastic derogatory remarks."
)

# Use in Predict module
toxicity_classifier = dspy.Predict(signature)
result = toxicity_classifier(comment="This is great!")
print(result.toxic)  # False
```

### Inline Signature Examples

```python
# Question answering
qa = dspy.Predict("question -> answer")
result = qa(question="What is the capital of France?")

# Sentiment analysis with type
sentiment = dspy.Predict("sentence -> sentiment: bool")
result = sentiment(sentence="I love this product!")

# Multi-input reasoning
reasoner = dspy.ChainOfThought("context, question -> reasoning, answer")
result = reasoner(
    context="Paris is the capital of France.",
    question="What is the capital?"
)

# Classification with choices
classifier = dspy.Predict("text, options: list[str] -> selected_option: int")
result = classifier(
    text="I'm feeling happy",
    options=["sad", "happy", "neutral"]
)
```

---

## Class-Based Signatures

Class-based signatures provide stronger type safety, better IDE support, and clearer documentation.

### Basic Class Signature

```python
class BasicQA(dspy.Signature):
    """Answer questions with short factoid answers."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField(desc="often between 1 and 5 words")

# Use in modules
qa = dspy.Predict(BasicQA)
result = qa(question="What is the color of the sky?")
print(result.answer)
```

### Field Descriptions

```python
class DetailedQA(dspy.Signature):
    """Answer questions with comprehensive responses."""

    context: str = dspy.InputField(desc="Background information for answering")
    question: str = dspy.InputField(desc="The question to answer")
    confidence: float = dspy.OutputField(desc="Confidence score between 0.0 and 1.0")
    answer: str = dspy.OutputField(desc="Detailed answer with supporting evidence")
```

### Default Values

```python
class ConfigurableAnalysis(dspy.Signature):
    """Analyze text with configurable depth."""

    text: str = dspy.InputField()
    depth: int = dspy.InputField(default=3, desc="Analysis depth from 1-5")
    analysis: str = dspy.OutputField()

# Can omit fields with defaults
analyzer = dspy.Predict(ConfigurableAnalysis)
result = analyzer(text="Sample text")  # Uses depth=3
```

### Field Aliases

```python
class AliasedSignature(dspy.Signature):
    """Signature with field aliases."""

    input_text: str = dspy.InputField(alias="text")
    output_result: str = dspy.OutputField(alias="result")

# Call with alias or original name
predict = dspy.Predict(AliasedSignature)
result = predict(text="Hello")  # Uses alias
```

---

## Input and Output Fields

### `dspy.InputField(**kwargs)`

Wrapper around `pydantic.Field` marking fields as inputs.

**Parameters:**
- `desc` (str): Field description for the model
- `default` (Any): Default value if not provided
- `alias` (str): Alternative field name
- All pydantic `Field` kwargs (gt, lt, min_length, max_length, etc.)

```python
class InputExample(dspy.Signature):
    """Example with various input field configurations."""

    # Basic input
    text: str = dspy.InputField()

    # With description
    context: str = dspy.InputField(desc="Background context for the task")

    # With default
    temperature: float = dspy.InputField(default=0.7, desc="Sampling temperature")

    # With validation (Pydantic)
    word_count: int = dspy.InputField(gt=0, lt=1000, desc="Maximum words")

    # With alias
    user_query: str = dspy.InputField(alias="query")

    output: str = dspy.OutputField()
```

### `dspy.OutputField(**kwargs)`

Wrapper around `pydantic.Field` marking fields as outputs.

**Parameters:**
- `desc` (str): Field description for the model
- `prefix` (str): Text prefix for the output field in prompts
- All pydantic `Field` kwargs

```python
class OutputExample(dspy.Signature):
    """Example with various output field configurations."""

    question: str = dspy.InputField()

    # Basic output
    answer: str = dspy.OutputField()

    # With description
    reasoning: str = dspy.OutputField(desc="Step-by-step reasoning process")

    # With prefix (used in prompt formatting)
    summary: str = dspy.OutputField(
        prefix="Summary:",
        desc="Brief summary in 1-2 sentences"
    )

    # Typed output
    confidence: float = dspy.OutputField(desc="Confidence between 0.0 and 1.0")
```

---

## Typed Outputs

DSPy 3.x supports rich type annotations for structured outputs.

### Primitive Types

```python
from typing import Literal

class PrimitiveTypes(dspy.Signature):
    """Examples of primitive typed outputs."""

    question: str = dspy.InputField()

    # String (default)
    answer: str = dspy.OutputField()

    # Boolean
    is_factual: bool = dspy.OutputField()

    # Integer
    word_count: int = dspy.OutputField()

    # Float
    confidence: float = dspy.OutputField()
```

### Literal Types

Constrain outputs to specific values:

```python
class EmotionClassifier(dspy.Signature):
    """Classify emotion in text."""

    sentence: str = dspy.InputField()
    sentiment: Literal['sadness', 'joy', 'love', 'anger', 'fear', 'surprise'] = dspy.OutputField()

predict = dspy.Predict(EmotionClassifier)
result = predict(sentence="I'm so happy today!")
print(result.sentiment)  # 'joy'
```

### List Types

```python
from typing import List, Literal

class CategoryClassifier(dspy.Signature):
    """Classify into multiple categories."""

    text: str = dspy.InputField()

    # List of strings
    tags: list[str] = dspy.OutputField(desc="Relevant tags")

    # List of Literals
    categories: List[Literal["emergency", "routine", "quality"]] = dspy.OutputField()

    # List of primitives
    scores: list[float] = dspy.OutputField()
```

### Dict Types

```python
class EvidenceExtractor(dspy.Signature):
    """Extract evidence for claims."""

    document: str = dspy.InputField()
    claims: list[str] = dspy.InputField()

    # Dict with typed values
    evidence: dict[str, list[str]] = dspy.OutputField(
        desc="Mapping from claim to supporting sentences"
    )

    # Dict with mixed types
    metadata: dict[str, str | int | float] = dspy.OutputField()
```

### Pydantic Models

Use Pydantic models for complex structured outputs:

```python
import pydantic

class QueryResult(pydantic.BaseModel):
    """Search result with metadata."""
    text: str
    score: float
    source: str

class SearchSignature(dspy.Signature):
    """Semantic search signature."""

    query: str = dspy.InputField()
    result: QueryResult = dspy.OutputField()

predict = dspy.Predict(SearchSignature)
result = predict(query="What is DSPy?")
print(result.result.text)
print(result.result.score)
```

### Nested Pydantic Models

```python
class Author(pydantic.BaseModel):
    name: str
    affiliation: str

class Paper(pydantic.BaseModel):
    title: str
    authors: list[Author]
    year: int
    abstract: str

class PaperExtraction(dspy.Signature):
    """Extract paper metadata from text."""

    text: str = dspy.InputField()
    paper: Paper = dspy.OutputField()

extractor = dspy.Predict(PaperExtraction)
result = extractor(text="...")
for author in result.paper.authors:
    print(f"{author.name} - {author.affiliation}")
```

### Custom Container Types

```python
class MyContainer:
    class Query(pydantic.BaseModel):
        text: str
        filters: dict[str, str]

    class Score(pydantic.BaseModel):
        relevance: float
        quality: float

class CustomSignature(dspy.Signature):
    """Signature with nested custom types."""

    query: MyContainer.Query = dspy.InputField()
    score: MyContainer.Score = dspy.OutputField()
```

---

## Signature Manipulation

All manipulation methods return new Signature classes (immutable pattern).

### `insert(index, name, field, type_)`

Insert a field at a specific position:

```python
class BaseSignature(dspy.Signature):
    """Base signature."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()

# Insert field at index 1 (between question and answer)
ExtendedSignature = BaseSignature.insert(
    1,
    "context",
    dspy.InputField(desc="Additional context"),
    str
)

predict = dspy.Predict(ExtendedSignature)
result = predict(question="What?", context="Background info")
```

### `prepend(name, field, type_)`

Add a field at the beginning:

```python
PrependedSignature = BaseSignature.prepend(
    "system_instruction",
    dspy.InputField(desc="System-level instruction"),
    str
)
```

### `append(name, field, type_)`

Add a field at the end:

```python
AppendedSignature = BaseSignature.append(
    "confidence",
    dspy.OutputField(desc="Confidence score"),
    float
)
```

### `delete(name)`

Remove a field:

```python
SimplifiedSignature = ExtendedSignature.delete("context")
```

### `with_instructions(instructions: str)`

Create new signature with updated instructions:

```python
class GenericQA(dspy.Signature):
    """Generic question answering."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()

# Create specialized version
MedicalQA = GenericQA.with_instructions(
    "Answer medical questions with accurate, evidence-based information. "
    "Always include caveats about consulting healthcare professionals."
)

medical_qa = dspy.Predict(MedicalQA)
```

### `with_updated_fields(name, type_, **kwargs)`

Modify field metadata:

```python
# Update field description
UpdatedSignature = BaseSignature.with_updated_fields(
    "answer",
    str,
    desc="Concise answer in 1-2 sentences",
    prefix="Answer:"
)

# Update field type
TypedSignature = BaseSignature.with_updated_fields(
    "answer",
    Literal["yes", "no", "uncertain"]
)
```

### Chaining Manipulations

```python
# Build complex signatures through chaining
ComplexSignature = (
    BaseSignature
    .prepend("domain", dspy.InputField(), str)
    .append("reasoning", dspy.OutputField(), str)
    .with_instructions("Provide detailed reasoning for domain-specific questions.")
    .with_updated_fields("answer", str, desc="Evidence-backed answer")
)
```

---

## Serialization

### `dump_state()`

Extract signature configuration:

```python
class MySignature(dspy.Signature):
    """My custom signature."""
    input: str = dspy.InputField(desc="Input text")
    output: str = dspy.OutputField(desc="Output text")

state = MySignature.dump_state()
print(state)
# {
#     'instructions': 'My custom signature.',
#     'fields': {
#         'input': {'desc': 'Input text', ...},
#         'output': {'desc': 'Output text', ...}
#     }
# }
```

### `load_state(state)`

Restore signature from state:

```python
state = {
    'instructions': 'Custom instructions',
    'fields': {
        'question': {'type': 'str', 'input': True, 'desc': 'The question'},
        'answer': {'type': 'str', 'input': False, 'desc': 'The answer'}
    }
}

RestoredSignature = dspy.Signature.load_state(state)
```

### `equals(other)`

Compare signatures by JSON schema:

```python
class Sig1(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()

class Sig2(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()

print(Sig1.equals(Sig2))  # True
```

---

## Multi-Modal Signatures

### Image Inputs

```python
class DogBreedClassifier(dspy.Signature):
    """Output the dog breed of the dog in the image."""

    image_1: dspy.Image = dspy.InputField(desc="An image of a dog")
    answer: str = dspy.OutputField(desc="The dog breed of the dog in the image")

# Use with vision models
lm = dspy.LM("openai/gpt-4o", api_key="...")
dspy.configure(lm=lm)

classifier = dspy.Predict(DogBreedClassifier)
result = classifier(image_1=dspy.Image("path/to/dog.jpg"))
print(result.answer)
```

### Multiple Images

```python
class ImageComparison(dspy.Signature):
    """Compare two images."""

    image_1: dspy.Image = dspy.InputField(desc="First image")
    image_2: dspy.Image = dspy.InputField(desc="Second image")
    comparison: str = dspy.OutputField(desc="Detailed comparison of the images")
    similarity_score: float = dspy.OutputField(desc="Similarity from 0.0 to 1.0")
```

### Image Sources

```python
# From file path
image = dspy.Image("path/to/image.jpg")

# From URL
image = dspy.Image("https://example.com/image.jpg")

# From bytes
with open("image.jpg", "rb") as f:
    image = dspy.Image(f.read())

# From PIL Image
from PIL import Image
pil_img = Image.open("image.jpg")
image = dspy.Image(pil_img)

# From data URI
image = dspy.Image("data:image/jpeg;base64,/9j/4AAQ...")
```

### Audio Inputs

```python
class AudioTranscription(dspy.Signature):
    """Transcribe audio to text."""

    audio: dspy.Audio = dspy.InputField(desc="Audio recording")
    transcription: str = dspy.OutputField(desc="Accurate transcription")

# From file
audio = dspy.Audio.from_file("path/to/audio.wav")

# From URL
audio = dspy.Audio.from_url("https://example.com/audio.mp3")

# From array
import numpy as np
audio_array = np.random.randn(16000)  # 1 second at 16kHz
audio = dspy.Audio.from_array(audio_array, sampling_rate=16000, format='wav')

transcriber = dspy.Predict(AudioTranscription)
result = transcriber(audio=audio)
```

---

## Signature Design Guidelines

### 1. Clear Instructions

Use the docstring or instructions parameter to guide model behavior:

```python
# Good: Clear, specific instructions
class GoodSignature(dspy.Signature):
    """Classify customer support tickets by urgency.

    Use 'high' for issues affecting service availability.
    Use 'medium' for feature requests or minor bugs.
    Use 'low' for general questions or documentation requests.
    """
    ticket_text: str = dspy.InputField()
    urgency: Literal['low', 'medium', 'high'] = dspy.OutputField()

# Bad: Vague instructions
class BadSignature(dspy.Signature):
    """Classify tickets."""
    ticket: str = dspy.InputField()
    priority: str = dspy.OutputField()
```

### 2. Descriptive Field Names

Choose self-explanatory field names:

```python
# Good: Clear field names
class GoodNaming(dspy.Signature):
    customer_message: str = dspy.InputField()
    sentiment_score: float = dspy.OutputField()
    response_category: Literal['complaint', 'question', 'praise'] = dspy.OutputField()

# Bad: Ambiguous names
class BadNaming(dspy.Signature):
    input: str = dspy.InputField()
    output1: float = dspy.OutputField()
    output2: str = dspy.OutputField()
```

### 3. Appropriate Type Constraints

Use Literal and typed outputs to constrain model behavior:

```python
# Good: Constrained output space
class ConstrainedClassifier(dspy.Signature):
    """Classify sentiment."""
    text: str = dspy.InputField()
    sentiment: Literal['positive', 'negative', 'neutral'] = dspy.OutputField()

# Less ideal: Unconstrained string
class UnconstrainedClassifier(dspy.Signature):
    """Classify sentiment."""
    text: str = dspy.InputField()
    sentiment: str = dspy.OutputField(desc="Should be positive, negative, or neutral")
```

### 4. Structured Outputs for Complex Tasks

Use Pydantic models for multi-field structured outputs:

```python
class Entity(pydantic.BaseModel):
    text: str
    type: Literal['person', 'organization', 'location']
    confidence: float

class NamedEntityRecognition(dspy.Signature):
    """Extract named entities from text."""

    text: str = dspy.InputField()
    entities: list[Entity] = dspy.OutputField(desc="All named entities found")
```

### 5. Provide Context Through Descriptions

Use `desc` parameter to guide the model:

```python
class WellDocumented(dspy.Signature):
    """Answer questions about scientific papers."""

    paper_abstract: str = dspy.InputField(
        desc="Abstract of the scientific paper"
    )
    question: str = dspy.InputField(
        desc="Specific question about methodology, results, or conclusions"
    )
    answer: str = dspy.OutputField(
        desc="Concise answer citing specific parts of the abstract"
    )
    citations: list[str] = dspy.OutputField(
        desc="Direct quotes from abstract supporting the answer"
    )
```

---

## CLIO Agent Usage

CLIO Agent uses signatures to define expert agent interfaces and reasoning patterns.

### Expert Signature Pattern

```python
# Example: DataExpert signature in CLIO Agent
class DataAnalysisSignature(dspy.Signature):
    """Analyze scientific data and generate insights.

    Use domain knowledge to identify patterns, anomalies, and relationships.
    Provide evidence-based conclusions with statistical support.
    """

    data_description: str = dspy.InputField(
        desc="Description of the dataset including format, size, and variables"
    )
    analysis_goal: str = dspy.InputField(
        desc="Specific analysis objective or research question"
    )
    context: str = dspy.InputField(
        default="",
        desc="Additional domain context or constraints"
    )

    reasoning: str = dspy.OutputField(
        desc="Step-by-step analytical reasoning process"
    )
    insights: list[str] = dspy.OutputField(
        desc="Key insights discovered from the data"
    )
    recommended_actions: list[str] = dspy.OutputField(
        desc="Suggested next steps or further analyses"
    )
```

### Routing Signature Pattern

```python
class RoutingSignature(dspy.Signature):
    """Route user queries to appropriate expert agents.

    Analyze query intent and select the most suitable expert
    based on capabilities and domain knowledge.
    """

    user_query: str = dspy.InputField(desc="User's input query")
    available_experts: list[str] = dspy.InputField(
        desc="List of available expert agent names"
    )
    expert_capabilities: dict[str, str] = dspy.InputField(
        desc="Mapping of expert names to their capabilities"
    )

    selected_expert: str = dspy.OutputField(
        desc="Name of the most appropriate expert"
    )
    reasoning: str = dspy.OutputField(
        desc="Explanation for expert selection"
    )
```

### Tool Signature Pattern

```python
class ToolSelectionSignature(dspy.Signature):
    """Select and configure appropriate tools for a task.

    Consider tool capabilities, input requirements, and expected outputs.
    """

    task_description: str = dspy.InputField()
    available_tools: list[dspy.Tool] = dspy.InputField()

    selected_tool: str = dspy.OutputField(desc="Name of the chosen tool")
    tool_arguments: dict[str, str] = dspy.OutputField(
        desc="Arguments to pass to the tool"
    )
    reasoning: str = dspy.OutputField()
```

### ARC Memory Integration

```python
class MemoryAugmentedSignature(dspy.Signature):
    """Answer questions using ARC memory system.

    Retrieve relevant context from memory before generating response.
    """

    query: str = dspy.InputField()
    memory_context: list[str] = dspy.InputField(
        desc="Relevant items retrieved from ARC memory"
    )
    conversation_history: dspy.History = dspy.InputField(
        default=dspy.History(messages=[])
    )

    answer: str = dspy.OutputField()
    confidence: float = dspy.OutputField()
    memory_items_used: list[int] = dspy.OutputField(
        desc="Indices of memory items that informed the answer"
    )
```

---

## Summary

DSPy signatures provide a powerful, type-safe way to define LM interfaces:

1. **Inline signatures** for rapid prototyping
2. **Class-based signatures** for production code
3. **Rich type system** with primitives, Literals, lists, dicts, and Pydantic models
4. **Immutable manipulation** for signature composition
5. **Multi-modal support** for images and audio
6. **Serialization** for caching and deployment

For CLIO Agent, signatures define the contracts between the orchestrator, expert agents, and tools, enabling structured reasoning, memory-augmented responses, and capability-based routing.
