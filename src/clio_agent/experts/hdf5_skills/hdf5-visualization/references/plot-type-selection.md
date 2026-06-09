# Plot Type Selection Guide

This reference provides comprehensive guidance on choosing appropriate plot types for HDF5 data based on dimensionality, semantic meaning, and data characteristics.

## Overview

Effective visualization requires matching plot types to data structure and meaning. This guide helps identify data patterns and select optimal matplotlib plot types.

## Decision Framework

Use this three-level decision process:

1. **Dimensionality**: Primary criterion - how many dimensions?
2. **Semantic meaning**: What does the data represent?
3. **Data characteristics**: Value distribution, sampling, density

## 1D Data Visualization

### Characteristics

- Shape: `(N,)` - single dimension
- Common examples: Time series, sensor traces, profiles, 1D signals
- Primary question: Is data ordered or unordered?

### Plot Type Selection

#### Line Plot (`ax.plot()`)

**Use when:**
- Data has inherent ordering (time series, spatial profiles)
- Showing trends, patterns, or evolution
- Comparing multiple sequences

**Best for:**
- Temperature over time
- Sensor readings at fixed intervals
- Cross-sections through higher-dimensional data
- Signal traces
- Cumulative distributions

**Example semantic patterns:**
```python
# Time series indicators
'/measurements/timeseries'
'/sensor_data/readings'
dset.attrs['units'] = 'seconds' or 'hours' or 'timestamps'

# Spatial profiles
'/profiles/vertical'
'/cross_section/x_axis'
dset.attrs['description'] = 'altitude profile'
```

**Code pattern:**
```python
import h5py
import matplotlib.pyplot as plt

with h5py.File('data.h5', 'r') as f:
    x = f['/time'][:]  # Coordinate axis
    y = f['/values'][:]

fig, ax = plt.subplots()
ax.plot(x, y, linewidth=1.5)
ax.set_xlabel('Time')
ax.set_ylabel('Value')
ax.grid(True, alpha=0.3)
plt.savefig('line_plot.png')
plt.close()
```

#### Histogram (`ax.hist()`)

**Use when:**
- Analyzing value distributions
- Understanding frequency patterns
- Data is unordered or order is irrelevant
- Exploring data characteristics before further analysis

**Best for:**
- Particle size distributions
- Measurement error analysis
- Pixel intensity distributions
- Quality control metrics
- Statistical analysis

**Example semantic patterns:**
```python
# Distribution indicators
'/measurements/samples'
'/quality/metrics'
'/pixels'  # When analyzing pixel values, not displaying image

# Check if data represents frequencies/counts
dset.attrs['description'] = 'distribution' or 'samples' or 'measurements'
```

**Code pattern:**
```python
import h5py
import matplotlib.pyplot as plt
import numpy as np

with h5py.File('data.h5', 'r') as f:
    data = f['/measurements'][:]

fig, ax = plt.subplots()
ax.hist(data, bins=50, edgecolor='black', alpha=0.7)
ax.set_xlabel('Value')
ax.set_ylabel('Frequency')
ax.set_title('Distribution')

# Add statistics
mean_val = np.mean(data)
std_val = np.std(data)
ax.axvline(mean_val, color='red', linestyle='--', label=f'Mean: {mean_val:.2f}')
ax.legend()

plt.savefig('histogram.png')
plt.close()
```

#### Scatter Plot (1D as function of index)

**Use when:**
- Showing individual measurements
- Highlighting outliers or discrete events
- Data is sparse or irregular

**Best for:**
- Event timestamps
- Sparse measurements
- Anomaly detection

## 2D Data Visualization

### Characteristics

- Shape: `(M, N)` - two dimensions
- Common examples: Spatial fields, images, matrices, grids
- Primary questions: Regular or irregular? Continuous or discrete?

### Plot Type Selection

#### Pcolormesh (`ax.pcolormesh()`)

**Use when:**
- Regular grid data (uniform or non-uniform spacing)
- Spatial fields with physical meaning
- Moderate to large datasets
- Need for accurate value representation

**Best for:**
- Temperature/pressure fields
- Geographic data
- Simulation outputs
- Scientific measurements on grids

**Example semantic patterns:**
```python
# Spatial field indicators
'/temperature/field'
'/simulation/grid_data'
'/earth_science/surface_temp'
dset.attrs['units'] = 'Kelvin' or 'Pascal' or 'meters'

# Check for coordinate arrays
'x_coordinates' in parent_group
'y_coordinates' in parent_group
```

**Code pattern:**
```python
import h5py
import matplotlib.pyplot as plt

with h5py.File('data.h5', 'r') as f:
    data = f['/temperature'][:]
    # Optional: Load coordinate arrays
    if 'x' in f['/temperature'].parent:
        x = f['/temperature'].parent['x'][:]
        y = f['/temperature'].parent['y'][:]
    else:
        x, y = None, None

fig, ax = plt.subplots(figsize=(10, 8))

if x is not None and y is not None:
    im = ax.pcolormesh(x, y, data, shading='auto', cmap='coolwarm')
else:
    im = ax.pcolormesh(data, shading='auto', cmap='coolwarm')

plt.colorbar(im, ax=ax, label='Temperature (K)')
ax.set_xlabel('X')
ax.set_ylabel('Y')
ax.set_title('Temperature Field')
plt.savefig('field.png', dpi=150)
plt.close()
```

#### Imshow (`ax.imshow()`)

**Use when:**
- Dense regular grids
- Image-like data
- Fast rendering needed (large arrays)
- Pixel-perfect representation desired

**Best for:**
- Images, photographs
- Dense matrices
- Correlation matrices
- Quick visualization of large grids

**Difference from pcolormesh:**
- Faster rendering
- Better for image-like data
- Fixed aspect ratio by default
- Treats data as pixels, not cell centers

**Example semantic patterns:**
```python
# Image indicators
'/image' or '/photo' or '/picture'
'/camera/sensor'
dset.attrs['type'] = 'image'

# Dense grid indicators
dset.shape[0] > 500 and dset.shape[1] > 500  # Large dense grid
```

**Code pattern:**
```python
import h5py
import matplotlib.pyplot as plt

with h5py.File('data.h5', 'r') as f:
    img = f['/image'][:]

fig, ax = plt.subplots()
im = ax.imshow(img, cmap='gray', origin='lower', aspect='auto')
plt.colorbar(im, ax=ax)
ax.set_xlabel('X pixel')
ax.set_ylabel('Y pixel')
plt.savefig('image.png', dpi=100)
plt.close()
```

**Origin parameter:**
- `origin='lower'`: (0,0) at bottom-left (scientific convention)
- `origin='upper'`: (0,0) at top-left (image convention)

#### Contour/Contourf (`ax.contour()`, `ax.contourf()`)

**Use when:**
- Emphasizing gradients and level sets
- Smooth continuous fields
- Publication-quality figures
- Need to show isolines explicitly

**Best for:**
- Topography
- Potential fields
- Smooth scientific data
- Weather maps

**Difference between contour and contourf:**
- `contour`: Line contours only
- `contourf`: Filled contours

**Example semantic patterns:**
```python
# Smooth field indicators
'/topography' or '/elevation'
'/potential' or '/field'
dset.attrs['description'] = 'continuous field'

# Data varies smoothly (check gradients)
```

**Code pattern:**
```python
import h5py
import matplotlib.pyplot as plt
import numpy as np

with h5py.File('data.h5', 'r') as f:
    data = f['/potential'][:]

fig, ax = plt.subplots()

# Filled contours
levels = np.linspace(data.min(), data.max(), 20)
cf = ax.contourf(data, levels=levels, cmap='viridis')
plt.colorbar(cf, ax=ax, label='Potential')

# Add contour lines
cs = ax.contour(data, levels=levels, colors='black', linewidths=0.5, alpha=0.3)
ax.clabel(cs, inline=True, fontsize=8)

ax.set_xlabel('X')
ax.set_ylabel('Y')
plt.savefig('contour.png', dpi=150)
plt.close()
```

#### Scatter (`ax.scatter()`)

**Use when:**
- Irregular or sparse 2D data
- Showing individual points
- Color/size encoding additional dimensions
- Discrete measurements

**Best for:**
- Point clouds
- Station data (weather stations, sensors)
- Particle positions
- Sparse sampling

**Example semantic patterns:**
```python
# Irregular data indicators
'/stations/locations'
'/particles/positions'
'/measurements/coordinates'

# Check if data represents discrete points
len(data.shape) == 2 and data.shape[1] in [2, 3]  # Nx2 or Nx3 array
```

**Code pattern:**
```python
import h5py
import matplotlib.pyplot as plt

with h5py.File('data.h5', 'r') as f:
    x = f['/points/x'][:]
    y = f['/points/y'][:]
    values = f['/points/values'][:]  # Optional: color by value

fig, ax = plt.subplots()
scatter = ax.scatter(x, y, c=values, cmap='plasma', s=50, alpha=0.7)
plt.colorbar(scatter, ax=ax, label='Value')
ax.set_xlabel('X')
ax.set_ylabel('Y')
plt.savefig('scatter.png')
plt.close()
```

## 3D and Higher-Dimensional Data

### Challenges

- Cannot visualize directly in 2D
- Must reduce dimensionality through slicing, projection, or aggregation
- Multiple visualization strategies possible

### Strategies

#### Strategy 1: Slicing

Extract 2D planes from 3D data:

```python
import h5py
import matplotlib.pyplot as plt

with h5py.File('data.h5', 'r') as f:
    # 3D data: shape (100, 200, 150)
    data_3d = f['/volume']

    # Extract middle slice along z-axis
    slice_z = data_3d[:, :, data_3d.shape[2] // 2]

fig, ax = plt.subplots()
im = ax.pcolormesh(slice_z, cmap='coolwarm')
plt.colorbar(im, ax=ax)
ax.set_title('Z-slice at midpoint')
plt.savefig('slice.png')
plt.close()
```

**Use slicing when:**
- Need to inspect specific locations
- Data represents volume (3D spatial field)
- Want to create multiple views

#### Strategy 2: Projection/Aggregation

Reduce dimension through statistical operations:

```python
import h5py
import matplotlib.pyplot as plt
import numpy as np

with h5py.File('data.h5', 'r') as f:
    data_3d = f['/volume'][:]

    # Project along z-axis (average)
    projection = np.mean(data_3d, axis=2)

fig, ax = plt.subplots()
im = ax.pcolormesh(projection, cmap='viridis')
plt.colorbar(im, ax=ax, label='Mean value')
ax.set_title('Z-axis projection (mean)')
plt.savefig('projection.png')
plt.close()
```

**Aggregation options:**
- `np.mean()`: Average value
- `np.max()`: Maximum projection
- `np.min()`: Minimum projection
- `np.sum()`: Total/integrated value
- `np.std()`: Variability

**Use projection when:**
- Want overview of entire volume
- Interested in integrated quantities
- Creating summary views

#### Strategy 3: Multiple Panels

Show several slices or views:

```python
import h5py
import matplotlib.pyplot as plt

with h5py.File('data.h5', 'r') as f:
    data_3d = f['/volume'][:]

fig, axes = plt.subplots(2, 2, figsize=(12, 10))

# Four slices along z
z_indices = [0, data_3d.shape[2]//3, 2*data_3d.shape[2]//3, -1]
for ax, z_idx in zip(axes.flat, z_indices):
    im = ax.pcolormesh(data_3d[:, :, z_idx], cmap='coolwarm')
    ax.set_title(f'Z = {z_idx}')
    plt.colorbar(im, ax=ax)

plt.tight_layout()
plt.savefig('multi_slice.png')
plt.close()
```

## Semantic Meaning Recognition

### Temperature/Thermal Data

**Indicators:**
- Name contains: 'temp', 'thermal', 'heat'
- Units: 'K', 'Kelvin', 'Celsius', 'C', 'Fahrenheit'
- Typical range: 200-400 K or 0-100°C

**Recommended visualization:**
- 2D: pcolormesh with 'coolwarm' or 'RdBu_r' colormap
- 1D: line plot for time series
- Diverging colormap centered on reference temperature

### Pressure Fields

**Indicators:**
- Name contains: 'pressure', 'press', 'p'
- Units: 'Pa', 'Pascal', 'bar', 'atm', 'psi'

**Recommended visualization:**
- 2D: contour/contourf for isobars
- Sequential colormap ('viridis', 'plasma')

### Time Series

**Indicators:**
- Dimension named 'time', 't', 'timestamp'
- Units: 'seconds', 'minutes', 'hours', 'days'
- 1D with ordered values

**Recommended visualization:**
- Line plot with time on x-axis
- Consider multiple subplots for related quantities
- Add grid for readability

### Images/Photos

**Indicators:**
- Name contains: 'image', 'photo', 'camera', 'picture'
- Large 2D array (typically >256x256)
- Values in [0, 255] range or [0, 1] range

**Recommended visualization:**
- imshow with 'gray' colormap for grayscale
- RGB if 3D with shape (M, N, 3)
- origin='upper' for conventional image orientation

### Spatial Grids

**Indicators:**
- Name contains: 'grid', 'mesh', 'field'
- Presence of coordinate arrays
- Regular spacing

**Recommended visualization:**
- pcolormesh for general fields
- contourf for smooth fields
- Include coordinate labels

### Distributions/Histograms

**Indicators:**
- Name contains: 'dist', 'histogram', 'frequency'
- Large 1D array or flattened data
- Analyzing statistics

**Recommended visualization:**
- Histogram with appropriate bin count
- Add statistical annotations (mean, std)
- Consider log scale for wide-range data

### Correlation/Similarity Matrices

**Indicators:**
- Square 2D array (N, N)
- Name contains: 'corr', 'correlation', 'similarity', 'distance'
- Values typically in [-1, 1] or [0, 1]

**Recommended visualization:**
- imshow with diverging colormap for correlation (-1 to 1)
- Sequential colormap for similarity/distance (0 to 1)
- Add colorbar with clear limits

## Colormap Selection

### Sequential Colormaps

Use for data with consistent direction (low to high):

- `'viridis'`: General-purpose, perceptually uniform
- `'plasma'`: High contrast
- `'cividis'`: Colorblind-friendly
- `'Blues'`, `'Greens'`: Single-hue

### Diverging Colormaps

Use for data with meaningful center point:

- `'coolwarm'`: Temperature, symmetric around zero
- `'RdBu_r'`: Red-blue diverging
- `'seismic'`: Centered on zero

### Specialized

- `'gray'`: Grayscale images
- `'jet'`: Avoid (not perceptually uniform)
- `'rainbow'`: Avoid (creates false features)

## Decision Tree Summary

```
Start
├─ 1D data?
│  ├─ Ordered (time, space)?
│  │  └─> Line plot
│  └─ Unordered or analyzing distribution?
│     └─> Histogram
│
├─ 2D data?
│  ├─ Image-like or very dense?
│  │  └─> imshow
│  ├─ Regular grid spatial field?
│  │  └─> pcolormesh
│  ├─ Smooth field needing gradients?
│  │  └─> contour/contourf
│  └─ Irregular points or sparse?
│     └─> scatter
│
└─ 3D+ data?
   ├─ Inspect specific location?
   │  └─> Slice to 2D
   ├─ Need overview?
   │  └─> Project/aggregate to 2D
   └─> Multiple views/panels
```

## Best Practices

1. **Check metadata first**: Let attributes guide visualization choices
2. **Match plot to semantics**: Temperature fields need different treatment than images
3. **Consider audience**: Scientific publication vs. quick inspection
4. **Use appropriate colormaps**: Perceptually uniform for quantitative data
5. **Label everything**: Axes, colorbars, titles with units
6. **Validate**: Does visualization accurately represent data?
7. **Test with subsets**: For large data, test with slices first
8. **Document choices**: Why this plot type? Why this colormap?

## Anti-Patterns to Avoid

- Using line plots for unordered data
- Using imshow for data that needs coordinate accuracy
- Using 'jet' colormap for scientific data
- Plotting 3D data without dimension reduction
- Missing colorbars on pseudocolor plots
- Ignoring aspect ratio for spatial data
- Using diverging colormaps for sequential data
- Forgetting axis labels and units
