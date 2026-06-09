---
name: hdf5-chunking
description: "rechunk dataset", "optimize chunk size", "fix chunk layout", "align chunks", "chunk cache", "slow partial reads", "chunk overhead", chunking strategies and storage layouts for HDF5 files.
version: 0.1.0
---

# HDF5 Chunking Strategies

## Purpose

Guidance for configuring HDF5 dataset chunking: choosing storage layouts, chunk sizes, chunk shapes, and chunk cache settings.

**Related skills**: `hdf5-filters` for compression, `hdf5-io` for write buffering.

## Storage Layouts

### Contiguous Storage
Data stored in single continuous block. Use when: dataset small (< 100 MB), always read entirely, fixed size, no compression needed. Convert with: `h5repack -l /dataset:CONTI input.h5 output.h5`

### Chunked Storage
Data divided into fixed-size chunks. Required for: compression, extensibility, partial access. Has metadata overhead.

## Chunk Size Guidelines

**Target: 10 KB to 1 MB per chunk**

- < 10 KB: Excessive overhead, metadata bloat
- 10-100 KB: Good for random access
- 100 KB-1 MB: Good for sequential access
- \> 1 MB: Wastes bandwidth, memory pressure

Calculate: `chunk_shape[0] × chunk_shape[1] × ... × dtype_size_bytes`

**Default to 100 KB chunks** when access pattern unknown.

## Chunk Shape: Align with Access Patterns

**Critical principle**: Match chunk shape to how data is accessed.

- **Row-wise** (`data[i, :]`): Row-oriented chunks `(1, 1000)` - NOT `(1000, 1)`
- **Column-wise** (`data[:, j]`): Column-oriented chunks `(1000, 1)` - NOT `(1, 1000)`
- **Block access** (`data[0:100, 0:100]`): Square chunks `(100, 100)`
- **3D slices** (along axis 0): Thin chunks `(1, 100, 100)`
- **3D volumes**: Balanced cubic `(10, 50, 50)`
- **Unknown**: Balanced dimensions proportional to dataset shape

## Rechunking with h5repack

```bash
# Basic rechunking
h5repack -l /dataset:CHUNK=100x200 input.h5 output.h5

# Multiple datasets
h5repack -l /raw:CHUNK=100x1x1 -l /grid:CHUNK=1x100x100 input.h5 output.h5

# With compression (see hdf5-filters)
h5repack -l /dataset:CHUNK=100x100 -f /dataset:SHUF -f /dataset:GZIP=3 input.h5 output.h5
```

## Chunk Cache Configuration

Configure when opening file for repeated/random access:

```python
import h5py

f = h5py.File('data.h5', 'r',
              rdcc_nbytes=10*1024**2,   # Cache size: 10 MB (default 8 MB)
              rdcc_nslots=10009,        # Hash slots: prime number (default 521)
              rdcc_w0=0.75)             # Preemption: 0.0-1.0 (default 0.75)
```

**rdcc_nbytes**: Cache size. Increase for random access, repeated reads. Rule of thumb: 10-50× chunk size.

**rdcc_nslots**: Hash table slots. Use prime number > expected chunks accessed. Common: 1009, 5003, 10009.

**rdcc_w0**: Preemption policy. 0.0 = LRU (sequential), 1.0 = LFU (repeated), 0.75 = balanced (usually best).

## Best Practices

- Align chunk shape with access pattern
- Target 10 KB - 1 MB per chunk
- Use contiguous storage for small datasets read entirely
- Increase cache for random access (10-50× chunk size)
- Use prime numbers for rdcc_nslots
- Test with realistic access patterns
- Document chunk choices and rationale

## Common Pitfalls

- Misaligned chunks (row chunks for column access)
- Too-small chunks (< 10 KB): metadata bloat
- Too-large chunks (> 1 MB): wastes bandwidth
- Insufficient cache for random access
