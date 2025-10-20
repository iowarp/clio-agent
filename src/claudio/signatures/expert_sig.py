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
    """Scientific data I/O expert for HDF5, ADIOS, Parquet optimization.

    Provides analysis and actionable recommendations for:
    - File format optimization (compression, chunking)
    - I/O performance tuning
    - Parallel HDF5/ADIOS configuration
    - Data layout strategies
    """

    # Input fields
    question: str = dspy.InputField(
        desc="User's question about scientific data files, formats, or I/O optimization"
    )
    file_context: str = dspy.InputField(
        desc="File information: paths, sizes, formats, access patterns, cluster configuration",
        default=""
    )

    # Output fields - structured like POC
    analysis: str = dspy.OutputField(
        desc="Technical analysis of the data I/O problem, file characteristics, and bottlenecks"
    )
    recommendations: str = dspy.OutputField(
        desc="Specific actionable recommendations: compression settings, chunking strategies, tools to use"
    )




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
        print(f"  Docstring: {sig_class.__doc__.split('.')[0]}...")

        print("  Input fields:")
        for field_name in sig_class.input_fields:
            print(f"    - {field_name}")

        print("  Output fields:")
        for field_name in sig_class.output_fields:
            print(f"    - {field_name}")

    print("\n" + "=" * 60)
    print("✅ Data expert signature defined")
    print("\nNext: Use this signature in DataExpert with dspy.ReAct")
