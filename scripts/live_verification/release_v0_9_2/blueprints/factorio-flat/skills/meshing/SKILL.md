---
name: meshing
title: Choose and Verify a Mesh for the Quantity of Interest
description: Match element type and density to what the study needs to resolve, and prove convergence rather than assuming a fine mesh is adequate.
---

Match mesh density to the quantity of interest, not the whole model
uniformly: a local stress concentration (fillet, hole, contact patch) needs
refinement proportional to the gradient there, while a global stiffness
result may not. A globally fine but locally under-resolved mesh can still
under-predict a peak stress.

Choose element type and order deliberately: linear vs. quadratic elements,
tetrahedral vs. hexahedral, and reduced- vs. full-integration formulations
each have known failure modes (shear locking, hourglassing) for specific
loading types — state which was chosen and why for the specific analysis.

Run and report a mesh convergence study for any quantity the conclusion
depends on: refine until the reported quantity changes by less than a stated
tolerance, and report that tolerance and the mesh density it was achieved at.
A single mesh density with no convergence check does not support a
quantitative conclusion.

Return the mesh (element type, density, local refinement) and the
convergence study — the quantity tracked, the refinement levels, and the
tolerance achieved — behind the reported result.
