#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "dspy-ai>=3.0.3",
#   "fastmcp>=2.13.0",
# ]
# ///

"""
ClioAgent Expert Signatures

Defines DSPy signatures for domain experts.
Each signature specifies the input/output interface for expert reasoning.

Available Signature:
- DataExpertSignature: Scientific data file optimization
"""

import dspy
import sys
from pathlib import Path

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent.parent  # src/clio_agent/signatures/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))


# ============================================================================
# DATA EXPERT SIGNATURE
# ============================================================================

class DataExpertSignature(dspy.Signature):
    """
    You are the CLIO Data Expert, a specialized agent within the CLIO Framework.
    Your goal is to provide deep technical analysis and actionable recommendations for scientific data challenges.

    Identity Rules:
    1. Identify as "CLIO Data Expert".
    2. Focus on HDF5, ADIOS, Parquet, and I/O performance.
    3. Be precise, technical, and data-driven.

    Input:
        - question: Question about data files or I/O
        - file_context: File details (path, size, type)

    Output:
        - analysis: Detailed technical analysis of the problem
        - recommendations: Specific, actionable optimization steps
    """

    # Input fields
    question: str = dspy.InputField()
    file_context: str = dspy.InputField()

    # Output fields
    analysis: str = dspy.OutputField()
    recommendations: str = dspy.OutputField()




# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("ClioAgent Expert Signatures Test")
    print("=" * 60)

    # List all expert signatures
    signatures = [
        ("DataExpert", DataExpertSignature),
    ]

    for name, sig_class in signatures:
        print(f"\n{name}:")
        doc = sig_class.__doc__.split('.')[0] if sig_class.__doc__ else 'No docstring'
        print(f"  Docstring: {doc}...")

        print("  Input fields:")
        for field_name in sig_class.input_fields:
            print(f"    - {field_name}")

        print("  Output fields:")
        for field_name in sig_class.output_fields:
            print(f"    - {field_name}")

    print("\n" + "=" * 60)
    print("✅ Data expert signature defined")
    print("\nNext: Use this signature in DataExpert with dspy.ReAct")
