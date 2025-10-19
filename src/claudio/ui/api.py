#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
#   "fastapi>=0.104.0",
#   "uvicorn>=0.24.0",
#   "sse-starlette>=1.6.0",
# ]
# ///

"""
ClaudIO FastAPI Server

REST API with Server-Sent Events (SSE) support for streaming responses.
Provides HTTP interface to ClaudIO for web integration.

Endpoints:
- POST /ask: Ask a question (blocking)
- POST /ask/stream: Ask with SSE streaming
- GET /experts: List available experts
- GET /stats: Get usage statistics
- GET /health: Health check

Example:
    # Run server
    $ uv run claudio/ui/api.py
    $ # Or: uvicorn claudio.ui.api:app --reload

    # Query from client
    $ curl -X POST http://localhost:8000/ask \\
           -H "Content-Type: application/json" \\
           -d '{"question": "How do I optimize HDF5?"}'
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, AsyncGenerator
import json
import sys
from pathlib import Path

# Add src to path
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent.parent
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

from claudio.config import setup_dspy
from claudio.orchestrator import ClaudIOOrchestrator
from claudio.experts import get_expert_capabilities


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class QuestionRequest(BaseModel):
    """Request model for /ask endpoint."""
    question: str
    context: Optional[str] = ""
    expert: Optional[str] = None  # Optional: force specific expert


class QuestionResponse(BaseModel):
    """Response model for /ask endpoint."""
    question: str
    expert: str
    answer: str
    routing_reasoning: Optional[str] = None


class ExpertInfo(BaseModel):
    """Expert capability information."""
    id: str
    name: str
    description: str
    keywords: list[str]


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    version: str
    experts_available: int


# ============================================================================
# APP INITIALIZATION
# ============================================================================

def create_app(
    use_lm_studio: bool = True,
    use_ollama: bool = False,
    use_openai: bool = False
) -> FastAPI:
    """Create FastAPI application.

    Args:
        use_lm_studio: Use LM Studio (default)
        use_ollama: Use Ollama
        use_openai: Use OpenAI

    Returns:
        Configured FastAPI app
    """
    app = FastAPI(
        title="ClaudIO API",
        description="Self-Evolving AI Agent for Scientific Computing",
        version="0.1.0"
    )

    # CORS middleware for web clients
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure appropriately for production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Initialize ClaudIO
    try:
        setup_dspy(
            use_lm_studio=use_lm_studio,
            use_ollama=use_ollama,
            use_openai=use_openai,
            verbose=False
        )
        app.state.orchestrator = ClaudIOOrchestrator()
    except Exception as e:
        print(f"Error initializing ClaudIO: {e}")
        print("\nEnsure LM Studio is running at http://100.127.255.164:1234")
        raise

    return app


# Create default app instance
app = create_app(use_lm_studio=True)


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/", tags=["General"])
async def root():
    """Root endpoint with API information."""
    return {
        "name": "ClaudIO API",
        "version": "0.1.0",
        "description": "Self-Evolving AI Agent for Scientific Computing",
        "endpoints": {
            "ask": "/ask",
            "ask_stream": "/ask/stream",
            "experts": "/experts",
            "stats": "/stats",
            "health": "/health"
        }
    }


@app.get("/health", response_model=HealthResponse, tags=["General"])
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "version": "0.1.0",
        "experts_available": 5
    }


@app.post("/ask", response_model=QuestionResponse, tags=["Query"])
async def ask_question(request: QuestionRequest):
    """Ask ClaudIO a question.

    Args:
        request: Question request with question text and optional context

    Returns:
        Question response with expert routing and answer

    Example:
        ```bash
        curl -X POST http://localhost:8000/ask \\
             -H "Content-Type: application/json" \\
             -d '{"question": "How do I optimize HDF5 compression?"}'
        ```
    """
    try:
        orchestrator: ClaudIOOrchestrator = app.state.orchestrator

        # Route and get answer
        result = orchestrator(
            question=request.question,
            context=request.context
        )

        # Note: Usage logging removed (not core v0.1.0 feature)

        return QuestionResponse(
            question=request.question,
            expert=result.selected_expert,
            answer=result.answer,
            routing_reasoning=getattr(result, 'routing_reasoning', None)
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ask/stream", tags=["Query"])
async def ask_question_stream(request: QuestionRequest):
    """Ask ClaudIO a question with streaming response (SSE).

    This endpoint streams the answer as it's generated, useful for
    web UIs that want to show progressive responses.

    Args:
        request: Question request

    Returns:
        Server-Sent Events stream

    Example:
        ```javascript
        const eventSource = new EventSource('/ask/stream?question=...');
        eventSource.onmessage = (event) => {
            console.log(event.data);
        };
        ```
    """
    async def event_generator() -> AsyncGenerator[str, None]:
        """Generate SSE events."""
        try:
            orchestrator: ClaudIOOrchestrator = app.state.orchestrator

            # Send routing event
            yield f"event: routing\\ndata: {{\"status\": \"routing\"}}\\n\\n"

            # Get answer
            result = orchestrator(
                question=request.question,
                context=request.context
            )

            # Send expert selection event
            expert_data = json.dumps({
                "expert": result.selected_expert,
                "reasoning": getattr(result, 'routing_reasoning', '')
            })
            yield f"event: expert\\ndata: {expert_data}\\n\\n"

            # TODO: Implement actual streaming from DSPy
            # For now, send answer in chunks
            answer = result.answer
            chunk_size = 50
            for i in range(0, len(answer), chunk_size):
                chunk = answer[i:i+chunk_size]
                chunk_data = json.dumps({"chunk": chunk})
                yield f"event: answer\\ndata: {chunk_data}\\n\\n"

            # Send completion event
            yield f"event: done\\ndata: {{\"status\": \"complete\"}}\\n\\n"

        except Exception as e:
            error_data = json.dumps({"error": str(e)})
            yield f"event: error\\ndata: {error_data}\\n\\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )


@app.get("/experts", response_model=list[ExpertInfo], tags=["Experts"])
async def list_experts():
    """List available experts and their capabilities.

    Returns:
        List of expert information
    """
    caps = get_expert_capabilities()

    experts = [
        ExpertInfo(
            id=expert_id,
            name=cap['name'],
            description=cap['description'],
            keywords=cap['keywords']
        )
        for expert_id, cap in caps.items()
    ]

    return experts


@app.get("/version", tags=["General"])
async def get_version():
    """Get ClaudIO version information.

    Returns:
        Version and system information
    """
    return {
        "version": "0.1.0",
        "system": "ClaudIO Multi-Agent System",
        "experts": list(get_expert_capabilities().keys()),
        "features": [
            "Multi-agent orchestration",
            "Expert routing",
            "Tool-augmented reasoning",
            "Local LM support"
        ]
    }


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    import argparse

    parser = argparse.ArgumentParser(description="ClaudIO FastAPI Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    parser.add_argument("--ollama", action="store_true", help="Use Ollama instead of LM Studio")
    parser.add_argument("--openai", action="store_true", help="Use OpenAI instead of LM Studio")

    args = parser.parse_args()

    # Create app with selected LM
    app = create_app(
        use_lm_studio=not args.ollama and not args.openai,
        use_ollama=args.ollama,
        use_openai=args.openai
    )

    print(f"\nStarting ClaudIO API server at http://{args.host}:{args.port}")
    print("API docs at: http://localhost:8000/docs\n")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload
    )
