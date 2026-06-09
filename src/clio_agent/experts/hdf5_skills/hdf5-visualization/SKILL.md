---
name: hdf5-visualization
description: "visualize HDF5 data", "plot a dataset", "create a graph", "show a heatmap", "make a histogram", "plot time series", "visualize temperature/sensor/array data", any data visualization from HDF5 files, choosing plot types, matplotlib.
version: 0.1.0
---

# HDF5 Data Visualization

## Purpose

This skill provides guidance for creating effective data visualizations from HDF5 files using matplotlib. It focuses on analyzing data characteristics to choose appropriate plot types, flexible visualization scripts.

## Core Concepts

### HDF5 Data Characteristics

Before visualizing, understand these key data properties:

1. **Dimensionality**: Number of axes (1D, 2D, 3D, etc.)
2. **Shape**: Size along each dimension
3. **Semantic meaning**: What the data represents (temperature field, time series, distribution, etc.)
4. **Value characteristics**: Range, distribution, presence of outliers

Access these through metadata tools or by reading dataset attributes.

### Visualization Workflow

Follow this three-step process:

1. **Analyze**: Examine data shape, metadata, and sample values
2. **Choose**: Select plot type based on dimensionality and semantics
3. **Implement**: Write matplotlib script with appropriate parameters

## Quick Plot Type Selection

| Data Type | Dimensionality | Plot Type | Use When |
|-----------|----------------|-----------|----------|
| Time series | 1D | line | Ordered sequential data |
| Distribution | 1D (flattened) | hist | Understanding value frequencies |
| Point cloud | 2D+ | scatter | Discrete points, relationships |
| Regular grid | 2D | pcolormesh | Spatial fields, regular sampling |
| Image-like | 2D | imshow | Dense grids, faster rendering |
| Smooth field | 2D | contour/contourf | Emphasizing gradients, isolines |
| 3D+ data | 3D+ | Slice to 2D first | Must reduce dimensions |

For detailed guidance on plot selection based on semantic meaning and data patterns, consult **`references/plot-type-selection.md`**.

## Implementation Strategy

### Step 1: Analyze Data

Start by examining the dataset:

```python
import h5py
import numpy as np

with h5py.File('file.h5', 'r') as f:
    dset = f['/path/to/dataset']

    # Check basic properties
    print(f"Shape: {dset.shape}")
    print(f"Dtype: {dset.dtype}")
    print(f"Ndim: {dset.ndim}")

    # Check metadata
    for key, val in dset.attrs.items():
        print(f"{key}: {val}")

    # Sample values (for small datasets or slices)
    if dset.size < 1e6:
        data = dset[:]
        print(f"Range: [{np.min(data)}, {np.max(data)}]")
```

### Step 2: Choose Plot Type

Use dimensionality as primary criterion:

**1D Data:**
- **Line plot**: Sequential data (time series, profiles, traces)
- **Histogram**: Distributions, frequency analysis

**2D Data:**
- **pcolormesh**: Regular grids, spatial fields (temperature, pressure)
- **imshow**: Image-like data, faster for dense grids
- **contour/contourf**: Emphasize gradients, smooth fields
- **scatter**: Irregular points, discrete measurements

**3D+ Data:**
- Slice to 2D first using indexing or aggregation
- Consider multiple 2D views or animations
- Use hdf5_slices for memory efficiency

### Step 3: Implement Visualization

Write a focused matplotlib script:

```python
import h5py
import matplotlib.pyplot as plt
import numpy as np

# Read data
with h5py.File('file.h5', 'r') as f:
    data = f['/dataset'][:]  # Add slicing if needed

# Create plot
fig, ax = plt.subplots(figsize=(10, 8))

# Plot based on type (example: 2D pcolormesh)
im = ax.pcolormesh(data)
plt.colorbar(im, ax=ax)

# Configure axes
ax.set_xlabel('X dimension')
ax.set_ylabel('Y dimension')
ax.set_title('Dataset Name')

# Save
plt.tight_layout()
plt.savefig('output.png', dpi=100, bbox_inches='tight')
plt.close()
```

## Memory Efficiency

For large datasets, apply slicing before loading:

```python
# Instead of loading entire dataset
data = dset[:]  # May cause memory issues

# Slice first, then load
data = dset[0:1000, ::10, 50]  # First 1000 along dim 0, every 10th along dim 1, index 50 along dim 2
```

Slicing strategies:
- **Downsampling**: Use step slicing `[::N]` to reduce resolution
- **Indexing**: Select specific indices along dimensions
- **Range selection**: Use `[start:stop]` for contiguous chunks

## Semantic Interpretation

Infer data meaning from:

1. **Attribute names**: Check for 'units', 'description', 'long_name'
2. **Dataset names**: '/temperature', '/sensors/timeseries' suggest meaning
3. **Value ranges**: Physical quantities have characteristic scales
4. **Dimension metadata**: Coordinate arrays, axis labels

Example:
```python
# Check semantic metadata
units = dset.attrs.get('units', 'unknown')
description = dset.attrs.get('description', '')

# Infer from name
if 'temp' in dset.name.lower():
    # Likely temperature data - use sequential colormap
    cmap = 'coolwarm'
elif 'pressure' in dset.name.lower():
    # Pressure field - consider log scale if wide range
    # ...
```

## Scaling Strategies

Choose axis scales based on data characteristics:

- **Linear**: Default for most data
- **Logarithmic**: Data spanning multiple orders of magnitude (e.g., 1 to 1,000,000)
- **Symmetric log**: Data crossing zero with wide range
- **Custom transforms**: Domain-specific scaling

Apply scaling:
```python
ax.set_xscale('log')  # Logarithmic x-axis
ax.set_yscale('linear')  # Linear y-axis
```

## Common Patterns

### Pattern 1: 1D Time Series

```python
import h5py
import matplotlib.pyplot as plt

with h5py.File('data.h5', 'r') as f:
    time = f['/time'][:]
    values = f['/sensor_data'][:]

fig, ax = plt.subplots()
ax.plot(time, values)
ax.set_xlabel('Time')
ax.set_ylabel('Sensor Value')
plt.savefig('timeseries.png')
plt.close()
```

### Pattern 2: 2D Spatial Field

```python
import h5py
import matplotlib.pyplot as plt

with h5py.File('data.h5', 'r') as f:
    field = f['/temperature'][:]

fig, ax = plt.subplots()
im = ax.pcolormesh(field, cmap='coolwarm')
plt.colorbar(im, ax=ax, label='Temperature (K)')
ax.set_xlabel('X')
ax.set_ylabel('Y')
plt.savefig('temperature_field.png', dpi=150)
plt.close()
```

### Pattern 3: Distribution Analysis

```python
import h5py
import matplotlib.pyplot as plt

with h5py.File('data.h5', 'r') as f:
    # Flatten multi-dimensional data for histogram
    data = f['/measurements'][:].flatten()

fig, ax = plt.subplots()
ax.hist(data, bins=50, edgecolor='black')
ax.set_xlabel('Value')
ax.set_ylabel('Frequency')
ax.set_title('Distribution')
plt.savefig('histogram.png')
plt.close()
```

## Advanced Techniques

For advanced matplotlib techniques, colormaps, subplots, and complex layouts, consult **`references/matplotlib-techniques.md`**.

## Additional Resources

### Reference Files

Detailed guidance on specific topics:

- **`references/plot-type-selection.md`** - Comprehensive guide on choosing plot types based on data semantics and structure
- **`references/matplotlib-techniques.md`** - Advanced matplotlib patterns, colormaps, memory optimization

### Examples

Working visualization scripts in `examples/`:

- **`examples/basic-visualization-workflow.md`** - Complete workflow from data analysis to plot creation
- **`examples/multi-panel-visualization.py`** - Creating multiple views of the same dataset

## Best Practices

1. **Always analyze first**: Check shape, dtype, and metadata before choosing plot type
2. **Start simple**: Use basic plots first, add complexity only when needed
3. **Handle memory**: Apply slicing for large datasets (>100 MB)
4. **Use semantic info**: Let metadata guide visualization choices
5. **Match plot to data**: Respect data dimensionality and meaning
6. **Save efficiently**: Use appropriate DPI and format for intended use
7. **Close figures**: Always call `plt.close()` to free memory
8. **Validate output**: Check that visualization accurately represents data

## Common Mistakes to Avoid

- Loading entire large datasets into memory without slicing
- Choosing plot type without considering dimensionality
- Ignoring semantic metadata that could inform visualization
- Using default parameters when data requires custom scaling
- Creating plots without axis labels or titles
- Forgetting to close figures in scripts processing many files

## Quick Troubleshooting

**Memory errors**: Apply slicing before loading data
**Blank plots**: Check data range, ensure values are not all identical
**Slow rendering**: Use `imshow` instead of `pcolormesh` for dense 2D data
**Distorted aspect**: Set `ax.set_aspect('equal')` for square pixels
**Missing colorbars**: Add with `plt.colorbar(im, ax=ax)`
**Poor contrast**: Try different colormaps or scale transformations
