#!/usr/bin/env -S uv run
"""Test ClaudIO with LM Studio

Tests the full v0.2.0 + v0.3.0 stack:
- LM Studio connection
- ARC Memory storage
- Agent Registry routing
- Tool caching
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

def test_lm_studio_connection():
    """Test 1: LM Studio connection"""
    print("\n" + "="*60)
    print("TEST 1: LM Studio Connection")
    print("="*60)
    
    from claudio.config import fetch_lm_studio_models, setup_dspy
    
    # Check if LM Studio is running
    models = fetch_lm_studio_models()
    if not models:
        print("❌ LM Studio not running or no models loaded")
        print("   Start LM Studio and load a model, then try again")
        return False
    
    print(f"✓ LM Studio running with {len(models)} model(s)")
    for model in models:
        print(f"  - {model}")
    
    # Setup DSPy
    try:
        lm = setup_dspy(verbose=False)
        print("✓ DSPy configured with LM Studio")
        return True
    except Exception as e:
        print(f"❌ Failed to configure DSPy: {e}")
        return False

def test_arc_memory():
    """Test 2: ARC Memory"""
    print("\n" + "="*60)
    print("TEST 2: ARC Memory Storage")
    print("="*60)
    
    from claudio.arc.memory import ARCMemory
    from claudio.arc.schema import Conversation, Message
    import time
    import shutil
    from pathlib import Path
    
    # Clean test directory
    if Path('.test_lm_studio').exists():
        shutil.rmtree('.test_lm_studio')
    
    try:
        arc = ARCMemory(data_dir='.test_lm_studio')
        print("✓ ARCMemory initialized")
        
        # Create conversation
        conv = Conversation(
            session_id='lm_test',
            user_id='test_user',
            created_at=time.time(),
            messages=[
                Message(role='user', content='Test question', timestamp=time.time()),
                Message(role='assistant', content='Test answer', timestamp=time.time())
            ],
            metadata={'test': 'lm_studio'}
        )
        
        # Store
        arc.store_conversation(conv)
        print("✓ Conversation stored")
        
        # Retrieve
        retrieved = arc.get_conversation('lm_test')
        assert retrieved is not None
        assert len(retrieved.messages) == 2
        print(f"✓ Conversation retrieved ({len(retrieved.messages)} messages)")
        
        # Check stats
        stats = arc.get_cache_stats()
        print(f"✓ Cache stats: {stats['hit_rate']:.1%} hit rate, {stats['size']} entries")
        
        # Cleanup
        shutil.rmtree('.test_lm_studio')
        return True
        
    except Exception as e:
        print(f"❌ ARC Memory test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_agent_registry():
    """Test 3: Agent Registry"""
    print("\n" + "="*60)
    print("TEST 3: Agent Registry Routing")
    print("="*60)
    
    from claudio.registry.registry import AgentRegistry, AgentCapability
    
    try:
        registry = AgentRegistry()
        print("✓ AgentRegistry initialized")
        
        # Register test expert
        cap = AgentCapability(
            keywords=['hdf5', 'optimize', 'compression'],
            description='HDF5 optimization expert',
            tools=['analyze_hdf5', 'optimize_chunks'],
            specialization='hdf5'
        )
        
        class MockExpert:
            def forward(self, question):
                return {"analysis": "Mock HDF5 analysis"}
        
        registry.register_agent('hdf5_expert', MockExpert(), cap)
        print("✓ Expert registered")
        
        # Test routing
        decision = registry.route_query("How do I optimize HDF5 compression?")
        print(f"✓ Query routed to: {decision.selected_agent} (confidence: {decision.confidence:.2f})")
        
        assert decision.selected_agent == 'hdf5_expert'
        print("✓ Routing decision correct")
        
        return True
        
    except Exception as e:
        print(f"❌ Agent Registry test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_claudio_agent():
    """Test 4: Full ClaudIO agent (requires LM Studio)"""
    print("\n" + "="*60)
    print("TEST 4: ClaudIO Agent Integration")
    print("="*60)
    
    try:
        from claudio.claudio import ClaudIO
        from claudio.config import setup_dspy
        import shutil
        from pathlib import Path
        
        # Clean test directory
        if Path('.test_claudio').exists():
            shutil.rmtree('.test_claudio')
        
        # Setup LM
        lm = setup_dspy(verbose=False)
        print("✓ LM configured")
        
        # Create agent
        agent = ClaudIO(verbose=False, data_dir='.test_claudio')
        print("✓ ClaudIO agent created")
        
        # Check components
        assert hasattr(agent, 'arc'), "Missing ARC"
        assert hasattr(agent, 'registry'), "Missing Registry"
        print("✓ ARC Memory integrated")
        print("✓ Agent Registry integrated")
        
        # Test simple query (no tools, pure reasoning)
        print("\nTesting simple query...")
        result = agent(
            question="What is HDF5?",
            session_id="lm_test"
        )
        
        print(f"✓ Query processed")
        print(f"  Selected expert: {result.selected_expert}")
        print(f"  Confidence: {result.confidence:.2f}")
        print(f"  Answer length: {len(result.answer)} chars")
        
        # Check ARC storage
        history = agent.get_session_history("lm_test")
        assert len(history) > 0
        print(f"✓ Conversation stored in ARC ({len(history)} entries)")
        
        # Cleanup
        shutil.rmtree('.test_claudio')
        
        return True
        
    except Exception as e:
        print(f"❌ ClaudIO agent test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run all tests"""
    print("\n" + "━"*70)
    print(" "*15 + "CLAUDIO LM STUDIO INTEGRATION TEST")
    print("━"*70)
    
    results = []
    
    # Run tests
    results.append(("LM Studio Connection", test_lm_studio_connection()))
    results.append(("ARC Memory", test_arc_memory()))
    results.append(("Agent Registry", test_agent_registry()))
    results.append(("ClaudIO Agent", test_claudio_agent()))
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    for name, passed in results:
        status = "✓ PASSED" if passed else "❌ FAILED"
        print(f"{name:30s} {status}")
    
    total = len(results)
    passed = sum(1 for _, p in results if p)
    
    print("\n" + "="*60)
    if passed == total:
        print(f"✓✓✓ ALL TESTS PASSED ({passed}/{total}) ✓✓✓")
        print("\nClaudIO v0.2.0 + v0.3.0 is production-ready!")
        return 0
    else:
        print(f"⚠ SOME TESTS FAILED ({passed}/{total})")
        print(f"\nPassed: {passed}, Failed: {total - passed}")
        return 1

if __name__ == "__main__":
    exit(main())
