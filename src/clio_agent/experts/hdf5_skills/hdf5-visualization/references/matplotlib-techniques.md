# Matplotlib Techniques for HDF5 Visualization

This reference covers advanced matplotlib patterns, memory optimization, and best practices for creating effective visualizations from HDF5 data.

## Memory-Efficient Data Loading

### Problem

HDF5 files can contain arrays too large to fit in memory. Loading entire datasets causes memory errors or system slowdowns.

### Solution: Strategic Slicing

Apply slicing before loading data into memory:

```python
import h5py

with h5py.File('large_file.h5', 'r') as f:
    dset = f['/huge_dataset']  # Shape: (10000, 5000, 3000)

    # DON'T: Load everything
    # data = dset[:]  # May crash with MemoryError

    # DO: Slice first, then load
    data = dset[0:1000, ::5, 1500]  # Much smaller subset
```

### Slicing Strategies

#### Downsampling

Reduce resolution by taking every Nth point:

```python
# Every 10th point along each dimension
data = dset[::10, ::10]

# Different sampling rates per dimension
data = dset[::5, ::2]  # Every 5th in dim 0, every 2nd in dim 1
```

**Use when:**
- Creating overview plots
- Data is over-sampled for visualization
- Need quick preview

#### Index Selection

Select specific slices along dimensions:

```python
# Middle slice along third dimension
data = dset[:, :, dset.shape[2] // 2]

# First and last along first dimension
data = dset[[0, -1], :, :]

# Specific indices
data = dset[[10, 50, 100], :, :]
```

**Use when:**
- Inspecting specific locations
- Creating multi-panel comparisons
- Extracting representative slices

#### Range Selection

Select contiguous ranges:

```python
# First 1000 points
data = dset[0:1000]

# Middle section
start = dset.shape[0] // 4
end = 3 * dset.shape[0] // 4
data = dset[start:end]
```

**Use when:**
- Focusing on region of interest
- Data quality varies by location
- Temporal or spatial subsets needed

### Memory Estimation

Estimate memory before loading:

```python
import h5py
import numpy as np

with h5py.File('file.h5', 'r') as f:
    dset = f['/dataset']

    # Calculate memory size
    nbytes = np.prod(dset.shape) * dset.dtype.itemsize
    mbytes = nbytes / (1024 ** 2)

    print(f"Dataset size: {mbytes:.1f} MB")

    # Decide whether to slice
    if mbytes > 100:
        print("Large dataset - apply slicing")
        # Apply appropriate slicing
    else:
        print("Small enough to load entirely")
        data = dset[:]
```

## Axis Configuration

### Scaling

Choose appropriate scale based on data range:

```python
import matplotlib.pyplot as plt
import numpy as np

fig, axes = plt.subplots(1, 3, figsize=(15, 5))

# Linear scale (default)
axes[0].plot(x, y)
axes[0].set_yscale('linear')
axes[0].set_title('Linear Scale')

# Logarithmic scale (for wide-range data)
axes[1].plot(x, y)
axes[1].set_yscale('log')
axes[1].set_title('Log Scale')

# Symmetric log (for data crossing zero)
axes[2].plot(x, y_with_negatives)
axes[2].set_yscale('symlog')
axes[2].set_title('Symmetric Log Scale')

plt.tight_layout()
plt.savefig('scales.png')
plt.close()
```

**Scale selection guide:**
- **Linear**: Default, data spans <2 orders of magnitude
- **Log**: Data spans multiple orders (1 to 1000, 0.01 to 100)
- **Symlog**: Data crosses zero with wide range
- **Logit**: Probability/proportion data (0 to 1)

### Limits

Control axis ranges for focus:

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots()
ax.pcolormesh(data)

# Set x and y limits
ax.set_xlim(10, 50)
ax.set_ylim(0, 100)

# Set limits from data percentiles (exclude outliers)
import numpy as np
vmin = np.percentile(data, 5)
vmax = np.percentile(data, 95)

# Apply to colormap
im = ax.pcolormesh(data, vmin=vmin, vmax=vmax)

plt.savefig('limited_range.png')
plt.close()
```

### Labels and Titles

Always include descriptive labels:

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots()
im = ax.pcolormesh(temperature_data, cmap='coolwarm')

# Axis labels with units
ax.set_xlabel('X Position (mm)')
ax.set_ylabel('Y Position (mm)')

# Title with metadata
ax.set_title('Temperature Field at t=10.5s')

# Colorbar with label and units
cbar = plt.colorbar(im, ax=ax)
cbar.set_label('Temperature (K)')

plt.savefig('labeled_plot.png')
plt.close()
```

## Subplot Layouts

### Regular Grids

Create multiple aligned plots:

```python
import matplotlib.pyplot as plt
import h5py

with h5py.File('data.h5', 'r') as f:
    # Load multiple datasets or slices
    data1 = f['/dataset1'][:]
    data2 = f['/dataset2'][:]
    data3 = f['/dataset3'][:]
    data4 = f['/dataset4'][:]

# Create 2x2 grid
fig, axes = plt.subplots(2, 2, figsize=(12, 10))

# Plot in each panel
datasets = [data1, data2, data3, data4]
titles = ['Dataset 1', 'Dataset 2', 'Dataset 3', 'Dataset 4']

for ax, data, title in zip(axes.flat, datasets, titles):
    im = ax.pcolormesh(data, cmap='viridis')
    ax.set_title(title)
    plt.colorbar(im, ax=ax)

plt.tight_layout()
plt.savefig('grid_layout.png')
plt.close()
```

### Shared Axes

Link axes for easier comparison:

```python
import matplotlib.pyplot as plt

fig, axes = plt.subplots(2, 1, figsize=(10, 8), sharex=True)

# Both subplots share x-axis
axes[0].plot(time, temperature)
axes[0].set_ylabel('Temperature (K)')

axes[1].plot(time, pressure)
axes[1].set_ylabel('Pressure (Pa)')
axes[1].set_xlabel('Time (s)')

plt.tight_layout()
plt.savefig('shared_axes.png')
plt.close()
```

### Custom Layouts with GridSpec

Create complex layouts:

```python
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

fig = plt.figure(figsize=(12, 8))
gs = GridSpec(3, 3, figure=fig)

# Large main plot
ax_main = fig.add_subplot(gs[0:2, 0:2])
ax_main.pcolormesh(main_data, cmap='viridis')
ax_main.set_title('Main View')

# Side histograms
ax_hist_x = fig.add_subplot(gs[2, 0:2])
ax_hist_x.hist(main_data.flatten(), bins=50)
ax_hist_x.set_xlabel('Value')

ax_hist_y = fig.add_subplot(gs[0:2, 2])
ax_hist_y.hist(main_data.flatten(), bins=50, orientation='horizontal')
ax_hist_y.set_ylabel('Value')

plt.tight_layout()
plt.savefig('complex_layout.png')
plt.close()
```

## Colormaps and Normalization

### Choosing Colormaps

Match colormap to data type:

```python
import matplotlib.pyplot as plt

fig, axes = plt.subplots(1, 3, figsize=(15, 5))

# Sequential: single direction
axes[0].pcolormesh(positive_data, cmap='viridis')
axes[0].set_title('Sequential (viridis)')

# Diverging: symmetric around center
axes[1].pcolormesh(symmetric_data, cmap='coolwarm')
axes[1].set_title('Diverging (coolwarm)')

# Categorical/Qualitative
axes[2].pcolormesh(categorical_data, cmap='tab10')
axes[2].set_title('Categorical (tab10)')

plt.tight_layout()
plt.savefig('colormaps.png')
plt.close()
```

**Recommended colormaps:**
- Sequential: `'viridis'`, `'plasma'`, `'cividis'` (perceptually uniform)
- Diverging: `'coolwarm'`, `'RdBu_r'`, `'seismic'`
- Grayscale: `'gray'`

**Avoid:**
- `'jet'`: Not perceptually uniform, creates false features
- `'rainbow'`: Similar issues to jet

### Custom Normalization

Control value-to-color mapping:

```python
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm, TwoSlopeNorm

fig, axes = plt.subplots(1, 3, figsize=(15, 5))

# Linear normalization (default)
axes[0].pcolormesh(data, cmap='viridis')
axes[0].set_title('Linear')

# Logarithmic normalization
axes[1].pcolormesh(data, cmap='viridis', norm=LogNorm(vmin=data.min(), vmax=data.max()))
axes[1].set_title('Log Normalized')

# Two-slope normalization (diverging at specific value)
norm = TwoSlopeNorm(vmin=data.min(), vcenter=0, vmax=data.max())
axes[2].pcolormesh(data, cmap='coolwarm', norm=norm)
axes[2].set_title('Centered at Zero')

plt.tight_layout()
plt.savefig('normalizations.png')
plt.close()
```

### Discrete Colormaps

Create distinct levels:

```python
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm
import numpy as np

# Define discrete levels
levels = np.linspace(data.min(), data.max(), 10)
norm = BoundaryNorm(levels, ncolors=256)

fig, ax = plt.subplots()
im = ax.pcolormesh(data, cmap='viridis', norm=norm)
cbar = plt.colorbar(im, ax=ax, ticks=levels)
cbar.set_label('Value')

plt.savefig('discrete_colormap.png')
plt.close()
```

## Figure Aesthetics

### Size and DPI

Control output quality:

```python
import matplotlib.pyplot as plt

# High-resolution for publication
fig, ax = plt.subplots(figsize=(10, 8))  # Size in inches
ax.plot(data)
plt.savefig('publication.png', dpi=300, bbox_inches='tight')
plt.close()

# Low-resolution for quick preview
fig, ax = plt.subplots(figsize=(8, 6))
ax.plot(data)
plt.savefig('preview.png', dpi=72)
plt.close()

# Vector format for scalability
fig, ax = plt.subplots(figsize=(10, 8))
ax.plot(data)
plt.savefig('vector.pdf')  # PDF, SVG, or EPS
plt.close()
```

**DPI guidelines:**
- **72-100**: Screen display, quick preview
- **150**: Standard documents
- **300**: Publication quality
- **600**: High-quality print

### Style Sheets

Apply consistent styling:

```python
import matplotlib.pyplot as plt

# Use built-in style
plt.style.use('seaborn-v0_8-darkgrid')

fig, ax = plt.subplots()
ax.plot(data)
plt.savefig('styled.png')
plt.close()

# Reset to default
plt.style.use('default')
```

**Common styles:**
- `'seaborn-v0_8'`: Clean scientific plots
- `'ggplot'`: R ggplot2 aesthetic
- `'bmh'`: Bayesian Methods for Hackers
- `'classic'`: Matplotlib classic

### Fonts and Text

Customize text appearance:

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots()
ax.plot(data)

# Configure fonts
ax.set_xlabel('X Axis', fontsize=14, fontweight='bold')
ax.set_ylabel('Y Axis', fontsize=14, fontweight='bold')
ax.set_title('Title', fontsize=16)

# Tick label size
ax.tick_params(axis='both', labelsize=12)

# Add text annotation
ax.text(0.5, 0.95, 'Annotation', transform=ax.transAxes,
        fontsize=12, verticalalignment='top',
        bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

plt.savefig('custom_fonts.png')
plt.close()
```

## Performance Optimization

### Rasterization

Speed up rendering of complex plots:

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots()

# Rasterize complex pcolormesh for faster rendering
im = ax.pcolormesh(large_data, cmap='viridis', rasterized=True)

# Vector elements (axes, labels) remain crisp
ax.set_xlabel('X')
ax.set_ylabel('Y')

plt.savefig('rasterized.pdf', dpi=150)
plt.close()
```

**Use when:**
- Plotting very large datasets
- Saving as vector format (PDF, SVG)
- File size is too large

### Efficient Plot Types

Choose faster plot types for large data:

```python
import matplotlib.pyplot as plt

# For very large 2D data:
# imshow is faster than pcolormesh
fig, ax = plt.subplots()
ax.imshow(large_data, cmap='viridis', aspect='auto', origin='lower')
plt.savefig('fast_2d.png')
plt.close()

# For 1D data with millions of points:
# Downsample or use rasterization
fig, ax = plt.subplots()
ax.plot(huge_1d_data[::100], rasterized=True)  # Every 100th point
plt.savefig('fast_1d.png')
plt.close()
```

### Memory Management

Close figures to free memory:

```python
import matplotlib.pyplot as plt

# Process multiple files
for filename in file_list:
    fig, ax = plt.subplots()
    # ... plotting code ...
    plt.savefig(f'output_{filename}.png')
    plt.close(fig)  # Important: free memory

# Or use context manager
for filename in file_list:
    with plt.ioff():  # Turn off interactive mode
        fig, ax = plt.subplots()
        # ... plotting code ...
        plt.savefig(f'output_{filename}.png')
        plt.close(fig)
```

## Common Patterns

### Pattern: Multi-Panel Time Series

```python
import h5py
import matplotlib.pyplot as plt

with h5py.File('timeseries.h5', 'r') as f:
    time = f['/time'][:]
    var1 = f['/variable1'][:]
    var2 = f['/variable2'][:]
    var3 = f['/variable3'][:]

fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

axes[0].plot(time, var1, 'b-', linewidth=1)
axes[0].set_ylabel('Variable 1', fontsize=12)
axes[0].grid(True, alpha=0.3)

axes[1].plot(time, var2, 'r-', linewidth=1)
axes[1].set_ylabel('Variable 2', fontsize=12)
axes[1].grid(True, alpha=0.3)

axes[2].plot(time, var3, 'g-', linewidth=1)
axes[2].set_ylabel('Variable 3', fontsize=12)
axes[2].set_xlabel('Time (s)', fontsize=12)
axes[2].grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig('timeseries_multipanel.png', dpi=150)
plt.close()
```

### Pattern: Field with Overlays

```python
import h5py
import matplotlib.pyplot as plt
import numpy as np

with h5py.File('field.h5', 'r') as f:
    field = f['/temperature'][:]
    x = f['/x'][:]
    y = f['/y'][:]

fig, ax = plt.subplots(figsize=(10, 8))

# Background field
im = ax.pcolormesh(x, y, field, cmap='coolwarm', shading='auto')
cbar = plt.colorbar(im, ax=ax, label='Temperature (K)')

# Overlay contour lines
levels = np.linspace(field.min(), field.max(), 10)
cs = ax.contour(x, y, field, levels=levels, colors='black', linewidths=0.5, alpha=0.5)
ax.clabel(cs, inline=True, fontsize=8)

ax.set_xlabel('X (m)')
ax.set_ylabel('Y (m)')
ax.set_title('Temperature Field with Isotherms')

plt.savefig('field_overlay.png', dpi=150)
plt.close()
```

### Pattern: Statistical Summary

```python
import h5py
import matplotlib.pyplot as plt
import numpy as np

with h5py.File('data.h5', 'r') as f:
    data = f['/measurements'][:]

fig, axes = plt.subplots(1, 2, figsize=(14, 6))

# Histogram with statistics
axes[0].hist(data.flatten(), bins=50, edgecolor='black', alpha=0.7)
axes[0].axvline(np.mean(data), color='red', linestyle='--', linewidth=2, label=f'Mean: {np.mean(data):.2f}')
axes[0].axvline(np.median(data), color='green', linestyle='--', linewidth=2, label=f'Median: {np.median(data):.2f}')
axes[0].set_xlabel('Value')
axes[0].set_ylabel('Frequency')
axes[0].legend()
axes[0].set_title('Distribution')

# Box plot
axes[1].boxplot(data.flatten())
axes[1].set_ylabel('Value')
axes[1].set_title('Box Plot')

plt.tight_layout()
plt.savefig('statistical_summary.png', dpi=150)
plt.close()
```

## Best Practices Summary

1. **Memory**: Slice large datasets before loading
2. **Scaling**: Use log scale for wide-range data
3. **Colormaps**: Choose perceptually uniform maps
4. **Labels**: Always include axes labels and units
5. **DPI**: 150-300 for publications, 72-100 for previews
6. **Cleanup**: Close figures after saving
7. **Layouts**: Use `tight_layout()` to prevent overlaps
8. **Formats**: PDF/SVG for vector, PNG for raster
9. **Performance**: Rasterize complex elements in vector formats
10. **Consistency**: Use style sheets for uniform appearance

## Troubleshooting

**Problem**: Memory error when loading data
**Solution**: Apply slicing before loading, estimate memory first

**Problem**: Plot renders slowly
**Solution**: Use imshow instead of pcolormesh, enable rasterization

**Problem**: Colormap doesn't show all values
**Solution**: Check vmin/vmax, use percentile-based normalization

**Problem**: Text overlaps in subplots
**Solution**: Call `plt.tight_layout()` before saving

**Problem**: File size too large
**Solution**: Rasterize plots, reduce DPI, use PNG instead of PDF

**Problem**: Colors look wrong
**Solution**: Check colormap choice, verify data range, check normalization

**Problem**: Can't see details in plot
**Solution**: Adjust figure size, increase DPI, focus on region with limits
