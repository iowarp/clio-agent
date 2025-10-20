#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO Expert Signatures

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
_src_root = _current_file.parent.parent.parent  # src/claudio/signatures/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))


# ============================================================================
# DATA EXPERT SIGNATURE
# ============================================================================

class DataExpertSignature(dspy.Signature):
    """Expert for scientific data I/O optimization.

    Input:
        - question: Question about data files or I/O
        - file_context: File details
        - history: Conversation history

    Output:
        - analysis: Technical analysis
        - recommendations: Actionable steps
    """

    # Input fields
    question: str = dspy.InputField(desc="Question about data I/O")
    file_context: str = dspy.InputField(desc="File details", default="")
    history: dspy.History = dspy.InputField(desc="Conversation history")

    # Output fields
    analysis: str = dspy.OutputField(desc="Technical analysis")
    recommendations: str = dspy.OutputField(desc="Actionable recommendations")




# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("ClaudIO Expert Signatures Test")
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
