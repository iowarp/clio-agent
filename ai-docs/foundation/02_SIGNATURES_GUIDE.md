# DSPy Signatures: Comprehensive Deep Research Report

## Executive Summary

DSPy Signatures are declarative specifications that define the input/output behavior of DSPy modules. They represent a paradigm shift from traditional prompt engineering by allowing developers to specify **what** a language model should accomplish rather than **how** to prompt it. This report synthesizes research from 15+ pages of official DSPy documentation at dspy.ai.

---

## Table of Contents

1. [What Are Signatures and Why They Matter](#what-are-signatures-and-why-they-matter)
2. [Signature Syntax and Declaration](#signature-syntax-and-declaration)
3. [Input/Output Field Specifications](#inputoutput-field-specifications)
4. [Type Hints and Constraints](#type-hints-and-constraints)
5. [Advanced Signature Patterns](#advanced-signature-patterns)
6. [Signature Composition](#signature-composition)
7. [Signature API Reference](#signature-api-reference)
8. [Complete Code Examples](#complete-code-examples)
9. [Best Practices and Anti-Patterns](#best-practices-and-anti-patterns)
10. [Pattern Library](#pattern-library)

---

## What Are Signatures and Why They Matter

### Definition

**A DSPy signature is a declarative specification of input/output behavior of a DSPy module.**

Signatures enable modular AI code where the DSPy compiler can optimize prompts or finetune models automatically, often producing better results than hand-crafted prompts.

### Core Philosophy

Signatures allow developers to:
- **Decouple interface from implementation**: Define what the task is, not how to prompt for it
- **Enable automatic optimization**: DSPy can infer or learn the best prompting strategy from data
- **Achieve modularity**: Signatures make code portable across different LMs, objectives, and pipelines
- **Avoid brittle prompts**: Move away from hacking long, fragile prompts that don't transfer well

### Why Signatures Matter

Traditional prompt engineering couples fundamental system architecture with incidental choices that aren't portable to new LMs, objectives, or pipelines. Signatures solve this by:

1. **Semantic clarity**: Field names express roles in plain English (e.g., `question` vs `answer`, `sql_query` vs `python_code`)
2. **Automatic prompt generation**: DSPy expands signatures into prompts based on the task specification
3. **Type-safe outputs**: Signatures enable parsing of typed outputs automatically
4. **Composability**: Signatures can be chained into ergonomic, portable, and optimizable AI systems

---

## Signature Syntax and Declaration

### Two Forms of Signatures

DSPy supports two syntax forms for defining signatures:

#### 1. **Inline (String-Based) Signatures**

Simple string syntax with optional type annotations for quick prototyping.

**Basic Format:**
```
"input_field -> output_field"
"input1, input2: type -> output1: type, output2: type"
```

**Examples:**
```python
# Simple QA
"question -> answer"  # Defaults to str types

# Sentiment analysis
"sentence -> sentiment: bool"

# Multi-input/output with types
"context: list[str], question: str -> answer: str"

# Complex multi-field
"question, choices: list[str] -> reasoning: str, selection: int"

# Summarization variations
"document -> summary"
"text -> gist"
"long_context -> tldr"

# Math with float output
"question -> answer: float"
```

**With Instructions:**
```python
dspy.Predict(
    dspy.Signature(
        "comment -> toxic: bool",
        instructions="Mark as 'toxic' if the comment includes insults, harassment, or sarcastic derogatory remarks."
    )
)
```

#### 2. **Class-Based Signatures**

For advanced tasks requiring verbose specifications with detailed documentation.

**Basic Structure:**
```python
class SignatureName(dspy.Signature):
    """Task description docstring."""

    input_field: type = dspy.InputField(desc="description")
    output_field: type = dspy.OutputField(desc="constraints")
```

**Complete Example:**
```python
class BasicQA(dspy.Signature):
    """Answer questions with short factoid answers."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField(desc="often between 1 and 5 words")
```

### Syntax Rules

1. **Arrow operator (`->`)**: Separates inputs from outputs
2. **Comma separation**: Multiple fields separated by commas
3. **Type annotations**: Use Python type syntax after colon (`: type`)
4. **Default type**: `str` if no type specified
5. **Field names**: Should express semantic roles in plain English
6. **Docstrings**: Describe the overall task in class-based signatures
7. **Instructions**: Can use variables at runtime for dynamic behavior

---

## Input/Output Field Specifications

### InputField

**Definition:**
```python
dspy.InputField(**kwargs)
```

**Purpose:** Marks a field as an input parameter and wraps Pydantic's `Field()` function with `__dspy_field_type="input"`.

**Common Parameters:**
- `desc`: Description/hint about the nature of the input
- Standard Pydantic Field parameters (default values, validation, etc.)

**Examples:**
```python
# Basic input
question: str = dspy.InputField()

# With description
context: str = dspy.InputField(desc="facts here are assumed to be true")

# Complex type
choices: list[str] = dspy.InputField(desc="available answer options")

# Multimodal inputs
image_1: dspy.Image = dspy.InputField(desc="An image of a dog")
passage_audio: dspy.Audio = dspy.InputField()
```

### OutputField

**Definition:**
```python
dspy.OutputField(**kwargs)
```

**Purpose:** Marks a field as an output and wraps Pydantic's `Field()` function with `__dspy_field_type="output"`.

**Common Parameters:**
- `desc`: Constraints or specifications for the output
- Standard Pydantic Field parameters

**Examples:**
```python
# Basic output
answer: str = dspy.OutputField()

# With constraint
answer: str = dspy.OutputField(desc="often between 1 and 5 words")

# Boolean classification
faithfulness: bool = dspy.OutputField()

# Complex structured output
entities_and_metadata: list[dict[str, str]] = dspy.OutputField()

# Constrained enum
sentiment: Literal['sadness', 'joy', 'love', 'anger', 'fear', 'surprise'] = dspy.OutputField()

# Numeric with Pydantic constraints
confidence: float = Field(ge=0, le=1, description="The confidence score for the answer")
```

### Special Field Types

#### 1. **dspy.Image**

For multimodal image inputs/outputs.

**Constructor:**
```python
dspy.Image(url: Any = None, *, download: bool = False, **data)
```

**Supported Formats:**
- HTTP(S)/GS URLs or local file paths
- Raw bytes
- PIL.Image instances
- Legacy dict format: `{"url": value}`
- Data URIs

**Example:**
```python
class DogPictureSignature(dspy.Signature):
    """Output the dog breed of the dog in the image."""

    image_1: dspy.Image = dspy.InputField(desc="An image of a dog")
    answer: str = dspy.OutputField(desc="The dog breed of the dog in the image")
```

#### 2. **dspy.Audio**

For audio inputs.

**Example:**
```python
class SpokenQASignature(dspy.Signature):
    """Answer the question based on the audio clip."""

    passage_audio: dspy.Audio = dspy.InputField()
    question: str = dspy.InputField()
    answer: str = dspy.OutputField(desc='factoid answer between 1 and 5 words')
```

#### 3. **dspy.History**

For conversational context.

**Structure:**
```python
class dspy.History(BaseModel):
    messages: list[dict[str, Any]]
```

**Example:**
```python
class QA(dspy.Signature):
    question: str = dspy.InputField()
    history: dspy.History = dspy.InputField()
    answer: str = dspy.OutputField()

# Usage
history = dspy.History(messages=[
    {"question": "What is the capital of France?", "answer": "Paris"},
    {"question": "What is the capital of Germany?", "answer": "Berlin"},
])

outputs = predict(question="What is the capital of Spain?", history=history)
```

#### 4. **dspy.Code**

For code generation and analysis.

**Syntax:**
```python
dspy.Code["language"]
```

**Example:**
```python
class CodeGeneration(dspy.Signature):
    """Generate python code to answer the question."""

    question: str = dspy.InputField(description="The question to answer")
    code: dspy.Code["java"] = dspy.OutputField(description="The code to execute")
```

**As input (code analysis):**
```python
class CodeAnalysis(dspy.Signature):
    """Analyze time complexity of code."""

    code: dspy.Code["python"] = dspy.InputField(description="The function to analyze")
    complexity: str = dspy.OutputField(description="Big-O time complexity")
```

---

## Type Hints and Constraints

### Supported Type Annotations

DSPy signatures support:

#### 1. **Basic Python Types**
```python
field: str
field: int
field: bool
field: float
```

#### 2. **Typing Module Types**
```python
from typing import Optional, Union, Literal

field: list[str]
field: dict[str, int]
field: Optional[float]
field: Union[str, int]
```

#### 3. **Literal Types** (for constrained outputs)
```python
class Emotion(dspy.Signature):
    """Classify emotion."""

    sentence: str = dspy.InputField()
    sentiment: Literal['sadness', 'joy', 'love', 'anger', 'fear', 'surprise'] = dspy.OutputField()
```

#### 4. **Enum Types**
```python
from enum import Enum

class EmailType(str, Enum):
    MARKETING = "marketing"
    SUPPORT = "support"
    SALES = "sales"

class ClassifyEmail(dspy.Signature):
    """Classify email type."""

    email_text: str = dspy.InputField()
    email_type: EmailType = dspy.OutputField()
```

#### 5. **Pydantic Models** (via TypedPredictor)
```python
from pydantic import BaseModel, Field

class Output(BaseModel):
    answer: str = Field(description="The answer to the question")
    confidence: float = Field(ge=0, le=1, description="Confidence score")

class QASignature(dspy.Signature):
    """Answer questions with confidence scores."""

    question: str = dspy.InputField()
    result: Output = dspy.OutputField()
```

#### 6. **Custom DSPy Types**
```python
# Special types
dspy.Image
dspy.Audio
dspy.History
dspy.Code["language"]
```

### Pydantic Field Constraints

For advanced validation using Pydantic's Field constraints:

```python
from pydantic import Field

class ScoredAnswer(dspy.Signature):
    """Answer with confidence scoring."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
    confidence: float = Field(
        ge=0,
        le=1,
        description="The confidence score for the answer"
    )
```

**Common Pydantic Constraints:**
- `ge`: Greater than or equal to
- `le`: Less than or equal to
- `gt`: Greater than
- `lt`: Less than
- `min_length`: Minimum string/list length
- `max_length`: Maximum string/list length
- `regex`: Regex pattern matching

---

## Advanced Signature Patterns

### 1. **Multi-Input Signatures**

```python
# String format
"context: list[str], question: str -> answer: str"

# Class format
class RAGSignature(dspy.Signature):
    """Answer questions using retrieved context."""

    context: list[str] = dspy.InputField(desc="Retrieved documents")
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
```

### 2. **Multi-Output Signatures**

```python
# String format
"question, choices: list[str] -> reasoning: str, selection: int"

# Class format
class MultiOutputSignature(dspy.Signature):
    """Answer with reasoning and selection."""

    question: str = dspy.InputField()
    choices: list[str] = dspy.InputField()
    reasoning: str = dspy.OutputField(desc="step-by-step reasoning")
    selection: int = dspy.OutputField(desc="index of selected choice")
```

### 3. **Chain-of-Thought Signatures**

ChainOfThought automatically adds a `reasoning` field to your signature:

```python
# Basic usage
classify = dspy.ChainOfThought('question -> answer', n=5)

# With custom signature
class BasicQA(dspy.Signature):
    """Answer questions with short factoid answers."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField(desc="often between 1 and 5 words")

generate_answer = dspy.ChainOfThought(BasicQA)

# ChainOfThought internally expands to:
# question -> reasoning, answer
```

### 4. **Conversational Signatures**

For multi-turn conversations:

```python
class ConversationalQA(dspy.Signature):
    """Answer questions with conversation history."""

    question: str = dspy.InputField()
    history: dspy.History = dspy.InputField()
    answer: str = dspy.OutputField()

# Usage
predict = dspy.Predict(ConversationalQA)

# First interaction
outputs = predict(
    question="What is the capital of France?",
    history=dspy.History(messages=[])
)

# Subsequent interaction with history
history = dspy.History(messages=[
    {"question": "What is the capital of France?", **outputs}
])
outputs2 = predict(
    question="What about Germany?",
    history=history
)
```

### 5. **Tool-Using Signatures (ReAct)**

```python
# Simple format
react = dspy.ReAct("question -> answer", tools=[get_weather, search_wikipedia])

# With float output
react = dspy.ReAct("question -> answer: float", tools=[evaluate_math, search_wikipedia])

# The ReAct module internally manages:
# - next_thought: reasoning
# - next_tool_name: selected tool
# - next_tool_args: JSON arguments
# - observation: tool result
```

### 6. **Judge/Verification Signatures**

```python
class FactJudge(dspy.Signature):
    """Judge if the answer is factually correct based on context."""

    context: str = dspy.InputField(desc="Context for the prediction")
    question: str = dspy.InputField(desc="Question to be answered")
    answer: str = dspy.InputField(desc="Answer for the question")
    factually_correct: bool = dspy.OutputField(
        desc="Is the answer factually correct based on context?"
    )
```

### 7. **Entity Extraction Signatures**

```python
class PeopleExtraction(dspy.Signature):
    """Extract people names from text."""

    text: str = dspy.InputField()
    people: list[str] = dspy.OutputField(desc="list of person names mentioned")
```

### 8. **Complex Structured Output**

```python
class StructuredExtraction(dspy.Signature):
    """Extract structured information from text."""

    text: str = dspy.InputField()
    title: str = dspy.OutputField()
    headings: list[str] = dspy.OutputField()
    entities_and_metadata: list[dict[str, str]] = dspy.OutputField()
```

### 9. **Repository Analysis Pipeline**

From the llms.txt generation tutorial:

```python
class AnalyzeRepository(dspy.Signature):
    """Analyze a repository structure and identify key components."""

    repo_url: str = dspy.InputField()
    file_tree: str = dspy.InputField()
    readme_content: str = dspy.InputField()
    project_purpose: str = dspy.OutputField()
    key_concepts: list[str] = dspy.OutputField()
    architecture_overview: str = dspy.OutputField()

class AnalyzeCodeStructure(dspy.Signature):
    """Analyze code structure to identify important directories and files."""

    file_tree: str = dspy.InputField()
    package_files: str = dspy.InputField()
    important_directories: list[str] = dspy.OutputField()
    entry_points: list[str] = dspy.OutputField()

class GenerateLLMsTxt(dspy.Signature):
    """Generate a comprehensive llms.txt file from analyzed repository information."""

    project_purpose: str = dspy.InputField()
    key_concepts: list[str] = dspy.InputField()
    architecture_overview: str = dspy.InputField()
    llms_txt_content: str = dspy.OutputField()
```

### 10. **Dynamic Runtime Signatures**

Signatures with runtime variable instructions:

```python
# Instructions can use variables at runtime
toxicity_checker = dspy.Predict(
    dspy.Signature(
        "comment -> toxic: bool",
        instructions="Mark as 'toxic' if the comment includes insults, harassment, or sarcastic derogatory remarks."
    )
)
```

---

## Signature Composition

### Chaining Signatures in Modules

Signatures are composed within custom DSPy modules:

```python
class RAG(dspy.Module):
    def __init__(self):
        # Multiple signatures in one module
        self.query_generator = dspy.Predict(QueryGenerator)
        self.answer_generator = dspy.ChainOfThought("question, context -> answer")

    def forward(self, question, **kwargs):
        # Compose signatures by passing outputs as inputs
        query = self.query_generator(question=question).query
        context = search_wikipedia(query)[0]
        return self.answer_generator(question=question, context=context).answer

class QueryGenerator(dspy.Signature):
    """Generate a query based on question to fetch relevant context."""

    question: str = dspy.InputField()
    query: str = dspy.OutputField()
```

### Multi-Expert Orchestration

```python
class MultiStepPipeline(dspy.Module):
    def __init__(self):
        self.generate_query = dspy.ChainOfThought('claim, notes -> query')
        self.append_notes = dspy.ChainOfThought(
            'claim, notes, context -> new_notes: list[str], titles: list[str]'
        )

    def forward(self, claim, notes):
        query = self.generate_query(claim=claim, notes=notes).query
        context = retrieve(query)
        result = self.append_notes(claim=claim, notes=notes, context=context)
        return result
```

### Nested Pipelines

All DSPy modules are built using `dspy.Predict`, which allows deep composition:

```python
class OuterModule(dspy.Module):
    def __init__(self):
        self.inner_module = InnerModule()  # Another dspy.Module
        self.final_step = dspy.ChainOfThought("intermediate -> final")

    def forward(self, input_data):
        intermediate = self.inner_module(input=input_data).output
        return self.final_step(intermediate=intermediate).final
```

---

## Signature API Reference

### dspy.Signature Class

**Inheritance:** Inherits from `pydantic.BaseModel`

**Purpose:** Base class for defining structured input/output specifications.

#### Class Methods

##### Field Manipulation

**`append(name, field, type_=None)`**
- Adds a field at the end of the signature's field list

**`prepend(name, field, type_=None)`**
- Inserts a field at the beginning of the signature's field list

**`insert(index, name, field, type_=None)`**
- Inserts a field at a specified position
- Supports negative indices for insertion from the end

**`delete(name)`**
- Removes a field by name from the signature

##### Field Updates

**`with_updated_fields(name, type_=None, **kwargs)`**
- Creates a new Signature class with updated field information
- Modifies `json_schema_extra` values
- Returns a new Signature class (immutable pattern)

**`with_instructions(instructions)`**
- Generates a new Signature class with modified instruction text

##### Comparison & State Management

**`equals(other)`**
- Compares the JSON schema of two Signature classes
- Checks instructions and field metadata

**`dump_state()`**
- Exports signature state including instructions and field prefixes/descriptions

**`load_state(state)`**
- Restores a signature from previously saved state data

#### Key Characteristics

- Supports dynamic field addition/removal at runtime
- Maintains separate input and output field collections
- Preserves field metadata through `json_schema_extra` attributes
- Enables signature composition and modification patterns

---

## Complete Code Examples

### Example 1: Simple Question Answering

```python
import dspy

dspy.configure(lm=dspy.LM("openai/gpt-4o-mini"))

# Inline signature
classify = dspy.Predict('question -> answer')
response = classify(question="What is the capital of France?")
print(response.answer)
```

### Example 2: Sentiment Classification with Confidence

```python
from typing import Literal

class SentimentClassification(dspy.Signature):
    """Classify the sentiment of a given sentence."""

    sentence: str = dspy.InputField()
    sentiment: Literal['positive', 'negative', 'neutral'] = dspy.OutputField()
    confidence: float = dspy.OutputField(desc="confidence score between 0 and 1")

# Use with ChainOfThought for reasoning
classifier = dspy.ChainOfThought(SentimentClassification)
result = classifier(sentence="I love this product!")

print(f"Sentiment: {result.sentiment}")
print(f"Confidence: {result.confidence}")
print(f"Reasoning: {result.reasoning}")
```

### Example 3: Multi-Modal Image Classification

```python
class DogBreedClassifier(dspy.Signature):
    """Identify the dog breed from an image."""

    image_1: dspy.Image = dspy.InputField(desc="An image of a dog")
    breed: str = dspy.OutputField(desc="The dog breed")
    confidence: float = dspy.OutputField(desc="Confidence score 0-1")

classifier = dspy.Predict(DogBreedClassifier)

# From file path
result = classifier(image_1=dspy.Image("/path/to/dog.jpg"))

# From URL
result = classifier(image_1=dspy.Image("https://example.com/dog.jpg"))

# From PIL Image
from PIL import Image
pil_img = Image.open("/path/to/dog.jpg")
result = classifier(image_1=dspy.Image(pil_img))
```

### Example 4: RAG Pipeline with Multiple Signatures

```python
class GenerateQuery(dspy.Signature):
    """Generate a search query from a question."""

    question: str = dspy.InputField()
    query: str = dspy.OutputField(desc="optimized search query")

class AnswerQuestion(dspy.Signature):
    """Answer question using retrieved context."""

    context: list[str] = dspy.InputField(desc="retrieved passages")
    question: str = dspy.InputField()
    answer: str = dspy.OutputField(desc="comprehensive answer")

class RAG(dspy.Module):
    def __init__(self, retriever):
        super().__init__()
        self.retriever = retriever
        self.query_gen = dspy.ChainOfThought(GenerateQuery)
        self.answer_gen = dspy.ChainOfThought(AnswerQuestion)

    def forward(self, question):
        query = self.query_gen(question=question).query
        context = self.retriever(query, k=5)
        answer = self.answer_gen(context=context, question=question).answer
        return dspy.Prediction(answer=answer, query=query)

# Usage
rag = RAG(retriever=ColBERTv2RetrieverModule())
result = rag(question="What is DSPy?")
print(result.answer)
```

### Example 5: Code Generation

```python
class CodeGeneration(dspy.Signature):
    """Generate python code to answer the question."""

    question: str = dspy.InputField(description="The question to answer")
    code: dspy.Code["python"] = dspy.OutputField(description="The code to execute")

predict = dspy.Predict(CodeGeneration)
result = predict(question="Given an array, find if any two numbers sum up to 10")
print(result.code)
```

### Example 6: Conversational Agent

```python
class ConversationalQA(dspy.Signature):
    """Answer questions with conversation history context."""

    question: str = dspy.InputField()
    history: dspy.History = dspy.InputField()
    answer: str = dspy.OutputField()

qa_bot = dspy.Predict(ConversationalQA)

# First turn
history = dspy.History(messages=[])
outputs = qa_bot(question="What is the capital of France?", history=history)
print(outputs.answer)  # "Paris"

# Update history
history.messages.append({"question": "What is the capital of France?", **outputs})

# Second turn with context
outputs = qa_bot(question="What about Germany?", history=history)
print(outputs.answer)  # "Berlin" (understanding "Germany" from context)
```

### Example 7: Tool-Using Agent with ReAct

```python
def get_weather(city: str) -> str:
    """Get the current weather for a city.

    Args:
        city: Name of the city

    Returns:
        Weather description
    """
    # Implementation here
    return f"Weather in {city}: Sunny, 72°F"

def search_wikipedia(query: str) -> str:
    """Search Wikipedia for information.

    Args:
        query: Search query

    Returns:
        Wikipedia summary
    """
    # Implementation here
    return "Wikipedia summary..."

# ReAct agent with tools
react = dspy.ReAct(
    signature="question -> answer",
    tools=[get_weather, search_wikipedia],
    max_iters=10
)

result = react(question="What's the weather like in Tokyo?")
print(result.answer)
```

### Example 8: ProgramOfThought for Math Reasoning

```python
dspy.configure(lm=dspy.LM('openai/gpt-4o-mini'))

# ProgramOfThought generates and executes Python code
pot = dspy.ProgramOfThought("question -> answer")

result = pot(question="If I have 3 apples and buy 2 more, then give away 1, how many do I have?")
print(result.answer)  # "4"
```

### Example 9: Typed Predictor with Pydantic Models

```python
from pydantic import BaseModel, Field

class Input(BaseModel):
    query: str = Field(description="User's question")

class Output(BaseModel):
    answer: str = Field(description="The answer to the question")
    confidence: float = Field(ge=0, le=1, description="Confidence score")
    sources: list[str] = Field(description="Source citations")

class TypedQASignature(dspy.Signature):
    """Answer questions with structured output."""

    input: Input = dspy.InputField()
    output: Output = dspy.OutputField()

# Use TypedPredictor instead of Predict
predictor = dspy.TypedPredictor(TypedQASignature)
# Or with string signature
predictor = dspy.TypedPredictor("input:Input -> output:Output")

result = predictor(input=Input(query="What is DSPy?"))
print(f"Answer: {result.output.answer}")
print(f"Confidence: {result.output.confidence}")
```

### Example 10: Few-Shot Learning with Demos

```python
class QA(dspy.Signature):
    """Answer questions based on context."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField()

predict = dspy.Predict(QA)

# Manually add few-shot examples
predict.demos.append(
    dspy.Example(
        question="What is the capital of France?",
        answer="The capital of France is Paris."
    )
)

predict.demos.append(
    dspy.Example(
        question="What is 2+2?",
        answer="4"
    )
)

# Now predictions use these examples as context
result = predict(question="What is the capital of Germany?")
print(result.answer)
```

### Example 11: Assertion-Based Validation

```python
class GenerateQuery(dspy.Signature):
    """Generate a search query from a claim."""

    claim: str = dspy.InputField()
    query: str = dspy.OutputField()

class QueryGenerator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.gen = dspy.ChainOfThought(GenerateQuery)

    def forward(self, claim):
        result = self.gen(claim=claim)

        # Assertion with dynamic signature modification on failure
        dspy.Suggest(
            len(result.query) < 100,
            "Query should be short and less than 100 characters."
        )

        return result

# When validation fails, DSPy modifies signature to include:
# - Past Output: rejected query
# - Instruction: "Query should be short and less than 100 characters."
# This enables intelligent retry
```

### Example 12: Batch Processing

```python
class Summarize(dspy.Signature):
    """Summarize the document."""

    document: str = dspy.InputField()
    summary: str = dspy.OutputField(desc="one paragraph summary")

summarizer = dspy.Predict(Summarize)

# Batch process multiple documents
documents = [
    dspy.Example(document="Long text 1..."),
    dspy.Example(document="Long text 2..."),
    dspy.Example(document="Long text 3..."),
]

results = summarizer.batch(documents)
for result in results:
    print(result.summary)
```

---

## Best Practices and Anti-Patterns

### Best Practices

#### 1. **Use Semantic Field Names**

✅ **Good:**
```python
class QA(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
```

❌ **Bad:**
```python
class QA(dspy.Signature):
    input: str = dspy.InputField()
    output: str = dspy.OutputField()
```

**Why:** Field names matter in DSPy. Express semantic roles in plain English: `question` is different from `answer`, `sql_query` is different from `python_code`.

#### 2. **Choose the Right Signature Form**

**Use inline signatures for:**
- Simple tasks
- Prototyping
- When field names and types are self-explanatory

```python
summarize = dspy.Predict("document -> summary")
```

**Use class-based signatures for:**
- Complex tasks requiring clarification
- Multiple input/output fields
- When constraints and descriptions are needed
- Production code

```python
class DetailedSummarization(dspy.Signature):
    """Create a comprehensive summary of the document."""

    document: str = dspy.InputField(desc="full text to summarize")
    max_length: int = dspy.InputField(desc="maximum summary length in words")
    summary: str = dspy.OutputField(desc="concise summary within length limit")
    key_points: list[str] = dspy.OutputField(desc="3-5 main takeaways")
```

#### 3. **Provide Clear Docstrings and Descriptions**

```python
class EmailClassifier(dspy.Signature):
    """Classify customer emails by type and urgency."""  # Clear task description

    email_text: str = dspy.InputField(desc="the full email content")
    email_type: Literal['support', 'sales', 'billing'] = dspy.OutputField(
        desc="primary category of the email"
    )
    urgency: Literal['low', 'medium', 'high'] = dspy.OutputField(
        desc="priority level requiring immediate attention"
    )
```

#### 4. **Use Type Constraints Appropriately**

```python
# Constrain outputs to valid options
sentiment: Literal['positive', 'negative', 'neutral'] = dspy.OutputField()

# Use numeric constraints for scores
confidence: float = Field(ge=0, le=1, description="Confidence score")

# Use list types for multiple items
entities: list[str] = dspy.OutputField(desc="extracted entity names")
```

#### 5. **Design for Composition**

```python
class StepOne(dspy.Signature):
    """First processing step."""
    raw_data: str = dspy.InputField()
    processed: str = dspy.OutputField()

class StepTwo(dspy.Signature):
    """Second processing step."""
    processed: str = dspy.InputField()
    final: str = dspy.OutputField()

class Pipeline(dspy.Module):
    def __init__(self):
        self.step1 = dspy.Predict(StepOne)
        self.step2 = dspy.Predict(StepTwo)

    def forward(self, raw_data):
        intermediate = self.step1(raw_data=raw_data).processed
        return self.step2(processed=intermediate).final
```

#### 6. **Leverage ChainOfThought for Complex Reasoning**

```python
# Instead of simple Predict for complex tasks
answer_gen = dspy.Predict("question -> answer")  # ❌ Misses reasoning

# Use ChainOfThought to get step-by-step thinking
answer_gen = dspy.ChainOfThought("question -> answer")  # ✅ Better results
```

#### 7. **Use Few-Shot Examples for Edge Cases**

```python
predictor = dspy.Predict(CustomSignature)

# Add examples for better performance
predictor.demos.append(dspy.Example(
    input_field="edge case input",
    output_field="desired output"
))
```

#### 8. **Validate with Assertions/Suggestions**

```python
def forward(self, text):
    result = self.classifier(text=text)

    # Soft constraint - allows retry
    dspy.Suggest(
        result.confidence > 0.7,
        "Confidence should be above 0.7 for reliable predictions."
    )

    return result
```

### Anti-Patterns

#### 1. **❌ Don't Hack Prompts Directly**

**Bad:**
```python
# Don't embed prompt instructions in your code logic
prompt = "You are a helpful assistant. Answer the question: {question}"
result = lm(prompt.format(question=question))
```

**Good:**
```python
# Let DSPy handle prompting through signatures
class QA(dspy.Signature):
    """Answer questions helpfully."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()

result = dspy.Predict(QA)(question=question)
```

#### 2. **❌ Don't Use Generic Field Names**

**Bad:**
```python
"input -> output"  # Too generic
```

**Good:**
```python
"user_query -> recommended_action"  # Semantic clarity
```

#### 3. **❌ Don't Couple Architecture with Incidental Choices**

**Bad:**
```python
# Hard-coding model-specific prompt patterns
if model == "gpt-4":
    signature = "question -> answer [format: JSON]"
else:
    signature = "Q: {question}\nA:"
```

**Good:**
```python
# Use consistent signature; let adapters handle formatting
signature = "question -> answer"
```

#### 4. **❌ Don't Over-Specify Simple Tasks**

**Bad:**
```python
class SimpleSummarize(dspy.Signature):
    """Summarize the document by extracting key information..."""  # Too verbose
    document: str = dspy.InputField(
        desc="input document requiring summarization via extraction..."
    )
    summary: str = dspy.OutputField(
        desc="output summary containing essential information..."
    )
```

**Good:**
```python
# Keep it simple when appropriate
"document -> summary"
```

#### 5. **❌ Don't Ignore Type Safety**

**Bad:**
```python
# Using str for everything
selection: str = dspy.OutputField()  # Could be "1", "one", "first"...
```

**Good:**
```python
# Use appropriate types
selection: int = dspy.OutputField(desc="0-indexed choice")
# Or
selection: Literal['first', 'second', 'third'] = dspy.OutputField()
```

#### 6. **❌ Don't Mix Concerns in Signatures**

**Bad:**
```python
class DoEverything(dspy.Signature):
    """Analyze sentiment, extract entities, and generate response."""
    text: str = dspy.InputField()
    sentiment: str = dspy.OutputField()
    entities: list[str] = dspy.OutputField()
    response: str = dspy.OutputField()
```

**Good:**
```python
# Separate concerns into focused signatures
class AnalyzeSentiment(dspy.Signature):
    """Analyze sentiment."""
    text: str = dspy.InputField()
    sentiment: str = dspy.OutputField()

class ExtractEntities(dspy.Signature):
    """Extract named entities."""
    text: str = dspy.InputField()
    entities: list[str] = dspy.OutputField()
```

---

## Pattern Library

### Pattern 1: Query Rewriting

```python
class RewriteQuery(dspy.Signature):
    """Rewrite user query for better search results."""

    original_query: str = dspy.InputField()
    rewritten_query: str = dspy.OutputField(desc="optimized search query")

# Usage in RAG pipeline
query_rewriter = dspy.ChainOfThought(RewriteQuery)
```

### Pattern 2: Multi-Hop Reasoning

```python
class GenerateSearchQuery(dspy.Signature):
    """Generate search query from claim and notes."""
    claim: str = dspy.InputField()
    notes: str = dspy.InputField()
    query: str = dspy.OutputField()

class SynthesizeAnswer(dspy.Signature):
    """Synthesize answer from accumulated evidence."""
    claim: str = dspy.InputField()
    evidence: list[str] = dspy.InputField()
    answer: str = dspy.OutputField()

class MultiHopQA(dspy.Module):
    def __init__(self, retriever, hops=3):
        self.retriever = retriever
        self.generate_query = dspy.ChainOfThought(GenerateSearchQuery)
        self.synthesize = dspy.ChainOfThought(SynthesizeAnswer)
        self.hops = hops

    def forward(self, claim):
        notes = ""
        evidence = []

        for _ in range(self.hops):
            query = self.generate_query(claim=claim, notes=notes).query
            passages = self.retriever(query, k=3)
            evidence.extend(passages)
            notes += f"\n{passages}"

        return self.synthesize(claim=claim, evidence=evidence)
```

### Pattern 3: Self-Consistency

```python
# Generate multiple answers and vote
class Answer(dspy.Signature):
    """Answer the question."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()

predictor = dspy.Predict(Answer, n=5)  # Generate 5 completions

# Process results
result = predictor(question="What is 2+2?")
# result contains multiple completions for voting
```

### Pattern 4: Decomposition

```python
class DecomposeQuestion(dspy.Signature):
    """Break complex question into simpler sub-questions."""

    complex_question: str = dspy.InputField()
    sub_questions: list[str] = dspy.OutputField(desc="2-4 simpler questions")

class AnswerSubQuestion(dspy.Signature):
    """Answer a single sub-question."""

    sub_question: str = dspy.InputField()
    answer: str = dspy.OutputField()

class SynthesizeAnswers(dspy.Signature):
    """Combine sub-answers into final answer."""

    original_question: str = dspy.InputField()
    sub_answers: list[str] = dspy.InputField()
    final_answer: str = dspy.OutputField()

class DecompositionQA(dspy.Module):
    def __init__(self):
        self.decompose = dspy.ChainOfThought(DecomposeQuestion)
        self.answer_sub = dspy.Predict(AnswerSubQuestion)
        self.synthesize = dspy.ChainOfThought(SynthesizeAnswers)

    def forward(self, complex_question):
        sub_questions = self.decompose(
            complex_question=complex_question
        ).sub_questions

        sub_answers = [
            self.answer_sub(sub_question=sq).answer
            for sq in sub_questions
        ]

        return self.synthesize(
            original_question=complex_question,
            sub_answers=sub_answers
        )
```

### Pattern 5: Reflection/Self-Refinement

```python
class GenerateAnswer(dspy.Signature):
    """Generate an initial answer."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()

class CritiqueAnswer(dspy.Signature):
    """Critique an answer for improvements."""

    question: str = dspy.InputField()
    answer: str = dspy.InputField()
    critique: str = dspy.OutputField(desc="areas for improvement")

class RefineAnswer(dspy.Signature):
    """Refine answer based on critique."""

    question: str = dspy.InputField()
    original_answer: str = dspy.InputField()
    critique: str = dspy.InputField()
    refined_answer: str = dspy.OutputField()

class SelfRefine(dspy.Module):
    def __init__(self, iterations=2):
        self.generate = dspy.ChainOfThought(GenerateAnswer)
        self.critique = dspy.ChainOfThought(CritiqueAnswer)
        self.refine = dspy.ChainOfThought(RefineAnswer)
        self.iterations = iterations

    def forward(self, question):
        answer = self.generate(question=question).answer

        for _ in range(self.iterations):
            critique = self.critique(
                question=question,
                answer=answer
            ).critique

            answer = self.refine(
                question=question,
                original_answer=answer,
                critique=critique
            ).refined_answer

        return dspy.Prediction(answer=answer)
```

### Pattern 6: Ensemble/Voting

```python
class Vote(dspy.Signature):
    """Vote on the best answer from candidates."""

    question: str = dspy.InputField()
    candidate_answers: list[str] = dspy.InputField()
    best_answer: str = dspy.OutputField()
    reasoning: str = dspy.OutputField(desc="why this answer is best")

class EnsembleQA(dspy.Module):
    def __init__(self, n_predictors=3):
        self.predictors = [
            dspy.ChainOfThought("question -> answer")
            for _ in range(n_predictors)
        ]
        self.voter = dspy.ChainOfThought(Vote)

    def forward(self, question):
        # Generate multiple answers
        candidate_answers = [
            p(question=question).answer
            for p in self.predictors
        ]

        # Vote for best
        return self.voter(
            question=question,
            candidate_answers=candidate_answers
        )
```

### Pattern 7: Conditional Routing

```python
class ClassifyIntent(dspy.Signature):
    """Classify user intent."""

    query: str = dspy.InputField()
    intent: Literal['factual_qa', 'opinion', 'command', 'chitchat'] = dspy.OutputField()

class Router(dspy.Module):
    def __init__(self):
        self.classifier = dspy.Predict(ClassifyIntent)
        self.factual_qa = dspy.ChainOfThought("question -> answer: str")
        self.opinion = dspy.ChainOfThought("question -> perspective: str")
        self.command = dspy.ChainOfThought("command -> action: str")
        self.chitchat = dspy.Predict("message -> response: str")

    def forward(self, query):
        intent = self.classifier(query=query).intent

        if intent == 'factual_qa':
            return self.factual_qa(question=query)
        elif intent == 'opinion':
            return self.opinion(question=query)
        elif intent == 'command':
            return self.command(command=query)
        else:
            return self.chitchat(message=query)
```

### Pattern 8: Iterative Refinement with Validation

```python
class ExtractData(dspy.Signature):
    """Extract structured data from text."""

    text: str = dspy.InputField()
    extracted_data: dict[str, str] = dspy.OutputField()

class IterativeExtractor(dspy.Module):
    def __init__(self, max_attempts=3):
        self.extractor = dspy.ChainOfThought(ExtractData)
        self.max_attempts = max_attempts

    def validate(self, data):
        """Check if extracted data meets requirements."""
        required_fields = ['name', 'email', 'phone']
        return all(field in data for field in required_fields)

    def forward(self, text):
        for attempt in range(self.max_attempts):
            result = self.extractor(text=text)

            if self.validate(result.extracted_data):
                return result

            # Add validation feedback to signature dynamically
            dspy.Suggest(
                False,
                f"Missing required fields. Ensure all of {required_fields} are extracted."
            )

        raise ValueError("Failed to extract valid data after max attempts")
```

### Pattern 9: Tool Selection and Execution

```python
class SelectTool(dspy.Signature):
    """Select the appropriate tool for the task."""

    task: str = dspy.InputField()
    available_tools: list[str] = dspy.InputField(desc="comma-separated tool names")
    selected_tool: str = dspy.OutputField()
    reasoning: str = dspy.OutputField(desc="why this tool was selected")

class ToolOrchestrator(dspy.Module):
    def __init__(self, tools):
        self.tools = {tool.__name__: tool for tool in tools}
        self.selector = dspy.ChainOfThought(SelectTool)

    def forward(self, task):
        # Select tool
        tool_names = list(self.tools.keys())
        selection = self.selector(
            task=task,
            available_tools=tool_names
        )

        # Execute selected tool
        selected_tool = self.tools[selection.selected_tool]
        result = selected_tool(task)

        return dspy.Prediction(
            result=result,
            tool_used=selection.selected_tool,
            reasoning=selection.reasoning
        )
```

### Pattern 10: Progressive Summarization

```python
class ChunkSummarize(dspy.Signature):
    """Summarize a text chunk."""

    chunk: str = dspy.InputField()
    summary: str = dspy.OutputField(desc="one paragraph summary")

class MergeSummaries(dspy.Signature):
    """Merge multiple summaries into one."""

    summaries: list[str] = dspy.InputField()
    merged_summary: str = dspy.OutputField(desc="coherent merged summary")

class ProgressiveSummarizer(dspy.Module):
    def __init__(self, chunk_size=1000):
        self.chunk_size = chunk_size
        self.chunk_summarizer = dspy.ChainOfThought(ChunkSummarize)
        self.merger = dspy.ChainOfThought(MergeSummaries)

    def forward(self, long_document):
        # Split into chunks
        chunks = [
            long_document[i:i+self.chunk_size]
            for i in range(0, len(long_document), self.chunk_size)
        ]

        # Summarize each chunk
        chunk_summaries = [
            self.chunk_summarizer(chunk=chunk).summary
            for chunk in chunks
        ]

        # Merge summaries
        if len(chunk_summaries) == 1:
            return dspy.Prediction(summary=chunk_summaries[0])

        return self.merger(summaries=chunk_summaries)
```

---

## Adapter Integration with Signatures

### How Adapters Work with Signatures

Adapters translate DSPy signatures into model-specific formats:

#### ChatAdapter (Default)

- Uses field delimiters: `[[ ## field_name ## ]]`
- Formats multi-turn messages
- Includes JSON schemas for complex types
- More verbose but universally compatible

#### JSONAdapter

- Prompts for JSON output containing all fields
- Uses native structured output when available
- Less verbose, lower latency
- Better for models supporting JSON mode

**Configuration:**
```python
# Global adapter
dspy.configure(adapter=dspy.adapters.JSONAdapter())

# Contextual adapter
with dspy.context(adapter=dspy.adapters.ChatAdapter()):
    result = predictor(question="...")
```

### Native Function Calling

For tool-using signatures:

- **ChatAdapter**: `use_native_function_calling=False` (text parsing)
- **JSONAdapter**: `use_native_function_calling=True` (native calls)

Can be overridden:
```python
adapter = dspy.adapters.ChatAdapter(use_native_function_calling=True)
```

---

## Optimization and Training

### BootstrapFewShot

Automatically generates few-shot examples for signatures:

```python
from dspy.teleprompt import BootstrapFewShot

class QA(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()

# Define metric
def validate_answer(example, prediction, trace=None):
    return example.answer.lower() in prediction.answer.lower()

# Compile with optimizer
optimizer = BootstrapFewShot(
    metric=validate_answer,
    max_bootstrapped_demos=4,
    max_labeled_demos=16
)

compiled_qa = optimizer.compile(
    student=dspy.Predict(QA),
    trainset=training_data
)
```

### MIPROv2

Optimizes both instructions and few-shot examples:

```python
from dspy.teleprompt import MIPROv2

optimizer = MIPROv2(
    metric=your_metric,
    num_candidates=10,
    init_temperature=1.0
)

optimized_program = optimizer.compile(
    program,
    trainset=train_data,
    num_trials=100
)
```

---

## Summary

DSPy Signatures represent a fundamental shift in how we build LM-powered applications:

### Key Takeaways

1. **Declarative over Imperative**: Specify what you want, not how to ask for it
2. **Type Safety**: Leverage Python's type system for structured outputs
3. **Composability**: Build complex pipelines from simple, focused signatures
4. **Optimization**: Enable automatic prompt engineering through signatures
5. **Portability**: Write once, optimize for any model

### Signature Design Principles

- **Semantic clarity**: Field names should express intent
- **Appropriate granularity**: One signature per conceptual task
- **Type constraints**: Use Literal, Enum, Pydantic models for structured outputs
- **Documentation**: Clear docstrings and field descriptions
- **Composition**: Design signatures to chain together naturally

### When to Use Each Form

| Use Case | Recommended Form |
|----------|------------------|
| Simple tasks, prototyping | Inline string signatures |
| Complex multi-field I/O | Class-based signatures |
| Need constraints/validation | Class-based with Pydantic |
| Multimodal inputs | Class-based with dspy.Image/Audio |
| Tool-using agents | ReAct with inline signature |
| Code generation | Class-based with dspy.Code |
| Conversational AI | Class-based with dspy.History |

---

## Additional Resources

- **Main Documentation**: https://dspy.ai/learn/programming/signatures/
- **API Reference**: https://dspy.ai/api/signatures/Signature/
- **Tutorials**: https://dspy.ai/tutorials/
- **Cheatsheet**: https://dspy.ai/cheatsheet/

---

**Research Date:** 2025-10-17
**DSPy Documentation Version:** Latest (as of January 2025)
**Pages Explored:** 15+ pages from dspy.ai official documentation
