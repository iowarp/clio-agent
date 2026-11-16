---
title: "A hybrid quantum classifier"
description: "Train a hybrid quantum-classical classifier beside a classical baseline, then see what a noisy quantum device does to its predictions, image by image."
source: https://clio.iowarp.ai/docs/examples/hybrid-qml/
---

# A hybrid quantum classifier

Train a hybrid quantum-classical classifier beside a classical baseline, then see what a noisy quantum device does to its predictions, image by image.

This example is a real recorded session with the [IBM Quantum](docs/ibm-quantum.md)
plugin. Asked a plain question, the agent built a small hybrid quantum-classical
classifier and a classical baseline, trained both, ran the hybrid on an exact
simulator and on a simulated noisy IBM device, and compared the two image by image.
Everything ran locally: no IBM account or key was used. You can open the whole
session below.

## The question

The session started from this request, with no tools or steps named:

```text
Build a hybrid quantum-classical classifier for MNIST 0 vs 1, compare it with a
classical baseline, and show me how hardware noise changes it
```

## How it ran

1. The agent loaded the plugin's hybrid quantum machine learning skill and built a
   Python environment for PyTorch and Qiskit in the workspace with CLIO's own `uv`.
2. Both models share a small classical front end that reduces each 28×28 image to
   two numbers. The hybrid feeds them to a two-qubit circuit (a ZZ feature map and
   one layer of trainable rotations) and reads ⟨ZZ⟩; the baseline uses a linear
   layer instead. Same data, epochs, optimizer and loss.
3. It published a live dashboard before training started, and the training script
   streamed each epoch's loss and accuracy to it.
4. After a first run, it checked the exact simulator's values against PyTorch,
   found that the quantum layer had been trained with an unintended 1/64 shot
   noise, and retrained with exact values.
5. It evaluated the trained hybrid on 100 test images as one batched estimator job
   on the exact simulator and one on `fake_fez`, a noisy simulation that uses the
   calibration data of IBM's Fez device.

![Two charts over 30 epochs: loss per epoch, where the classical model drops near 0 and the hybrid levels off near 0.66, and training accuracy per epoch, where the classical model reaches 1 and the hybrid stays near 0.78](plugin-asset://guide/captures/training-dashboard.webp)
*The training dashboard, filled live as each epoch finished. The classical baseline (pink) learns the task in one epoch; the hybrid (blue) levels off.*

## What it found

| Model | Run on | Test accuracy |
| --- | --- | --- |
| Classical baseline | PyTorch | 100% |
| Hybrid | Exact simulator | 73% |
| Hybrid | `fake_fez` noisy simulator | 73% |

Noise pulled the hybrid's outputs toward zero, by about 12% (a fitted slope of 0.88),
with a mean shift of 0.052. No image changed class. Five images sit within 0.05 of
the decision threshold, so they are the ones a stronger noise level could flip.

The agent stated the limits itself: the two-qubit head underfits, the noisy run
used one seed and one calibration snapshot, and the result is not evidence of a
quantum advantage.

## The per-image comparison

Asked to show how the noisy results compare with the exact ones for each test
image, the agent opened the plugin's results view: one point per image, exact and
noisy side by side, the shape marking the true class and a dashed line the class
threshold.

![IBM Quantum results view: expectation value for each of 100 parameter sets, exact in blue and fake_fez noisy in orange, circles for class 0 and squares for class 1, a dashed threshold at 0, and Accuracy exact 73% and fake_fez noisy 73%](plugin-asset://guide/captures/results-view.webp)
*The results view. Each image appears twice, exact and noisy; the noisy point sits a little closer to the threshold.*

## Ask about one image

Clicking a point selects that image, and the agent sees the selection. Here image 6
was selected, the image nearest the threshold, and the question was simply:
*"Why does this image sit so close to the boundary, and could noise flip it?"*

![Results view with parameter set 6 selected: exact 0.028, fake_fez noisy 0.037, expected class 0, and a tooltip for that point](plugin-asset://guide/captures/selected-image.webp)
*The selection reaches the agent as the image, both values and its true class.*

![The agent's answer: image 6 is a true 0 the model already gets wrong; its features land near the boundary; noise moved it from 0.028 to 0.037, away from a flip, which would take a move of about two standard errors](plugin-asset://guide/captures/boundary-answer.webp)
*The answer works from the selected values, the trained weights and the noisy run, and says what it has not shown.*

## Open the session

- [Open the hybrid classifier session](https://clio.iowarp.ai/conversations/quantum/hybrid-qml/): The recorded session: the conversation, every tool call, the loaded skill and the training dashboard. 15 MB.

The page is an interactive archive of the actual session. Opening it does not run
the agent or contact any service. The plugin's interactive views run only in a live
session, so the results view appears above as captures rather than in the archive.

A real-hardware run of the same evaluation needs an IBM Quantum account; see
[IBM Quantum](docs/ibm-quantum.md) for how CLIO asks for the key and checks the time
left before it submits.
