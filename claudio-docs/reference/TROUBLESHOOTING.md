---
title: "Troubleshooting"
category: reference
priority: medium
prerequisites: []
related:
  - foundation/05_LM_INTEGRATION.md
implementation_phase: 1
estimated_reading_time: "20 minutes"
version: "1.0"
---

# Troubleshooting Guide

## Connection Issues

**"Connection refused"**
- Check: LM Studio running? Port 1234?
- Fix: Start LM Studio, load model, click "Start Server"

**"ModuleNotFoundError: dspy"**
- Fix: `pip install dspy-ai`

**"OpenAI API key invalid"**
- Fix: `export OPENAI_API_KEY='sk-...'`

## Optimization Issues

**"Not enough examples"**
- Need: 10+ for BootstrapFewShot, 200+ for MIPROv2
- Fix: Collect more examples or use synthetic data

**"Optimization too expensive"**
- Fix: Use GPT-4o-mini instead of GPT-4
- Fix: Reduce num_trials parameter

**"Metric returns NaN"**
- Fix: Check metric function handles edge cases

## Performance Issues

**"Module slow"**
- Fix: Reduce max_iters in ReAct agents
- Fix: Use local LM (Ollama) instead of cloud

**"Tool calls failing"**
- Fix: Add error handling to tool wrappers
- Fix: Check tool input parameters

## Deployment Issues

**"Module won't load"**
- Fix: Check save/load file paths
- Fix: Verify DSPy version compatibility

---

Can't find solution? Check foundation/ docs or research/.
