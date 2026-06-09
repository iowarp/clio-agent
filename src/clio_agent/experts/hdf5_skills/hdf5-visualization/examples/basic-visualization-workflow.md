# Basic Visualization Workflow

This example demonstrates the complete workflow for visualizing HDF5 data, from initial analysis through final plot creation.

## Scenario

Visualize a 2D temperature field from a simulation output file.

## Step 1: Analyze the Data

First, examine the dataset to understand its structure:

```python
import h5py
import numpy as np

# Open file and examine structure
with h5py.File('simulation_output.h5', 'r') as f:
    # List all datasets
    print("Datasets in file:")
    f.visit(lambda name: print(name) if isinstance(f[name], h5py.Dataset) else None)

    # Examine the temperature dataset
    temp_dset = f['/results/temperature']

    print(f"\nDataset: /results/temperature")
    print(f"Shape: {temp_dset.shape}")
    print(f"Dtype: {temp_dset.dtype}")
    print(f"Chunks: {temp_dset.chunks}")

    # Check attributes
    print(f"\nAttributes:")
    for key, val in temp_dset.attrs.items():
        print(f"  {key}: {val}")

    # Sample values (check if we can load entire dataset)
    size_mb = np.prod(temp_dset.shape) * temp_dset.dtype.itemsize / (1024**2)
    print(f"\nDataset size: {size_mb:.1f} MB")

    if size_mb < 100:  # Safe to load
        data = temp_dset[:]
        print(f"Value range: [{np.min(data):.2f}, {np.max(data):.2f}]")
        print(f"Mean: {np.mean(data):.2f}")
        print(f"Std: {np.std(data):.2f}")
```

**Output:**
```
Datasets in file:
results/temperature
results/pressure
coordinates/x
coordinates/y

Dataset: /results/temperature
Shape: (200, 300)
Dtype: float64
Chunks: (50, 75)

Attributes:
  units: Kelvin
  description: Temperature field at final timestep
  time: 10.5

Dataset size: 0.5 MB
Value range: [273.15, 373.15]
Mean: 323.15
Std: 25.30
```

## Step 2: Choose Plot Type

Based on analysis:
- **Dimensionality**: 2D (200 × 300)
- **Semantic meaning**: Temperature field (spatial data)
- **Size**: Small enough to load entirely
- **Attributes**: Units in Kelvin, represents physical field

**Decision**: Use `pcolormesh` with diverging colormap (`coolwarm`) for temperature data.

## Step 3: Implement Basic Visualization

Create a simple visualization:

```python
import h5py
import matplotlib.pyplot as plt

# Read data
with h5py.File('simulation_output.h5', 'r') as f:
    temperature = f['/results/temperature'][:]
    x = f['/coordinates/x'][:]
    y = f['/coordinates/y'][:]
    units = f['/results/temperature'].attrs['units']
    time = f['/results/temperature'].attrs['time']

# Create plot
fig, ax = plt.subplots(figsize=(10, 8))

# Plot temperature field
im = ax.pcolormesh(x, y, temperature, cmap='coolwarm', shading='auto')

# Add colorbar
cbar = plt.colorbar(im, ax=ax)
cbar.set_label(f'Temperature ({units})', fontsize=12)

# Labels and title
ax.set_xlabel('X Position (m)', fontsize=12)
ax.set_ylabel('Y Position (m)', fontsize=12)
ax.set_title(f'Temperature Field at t={time}s', fontsize=14)

# Equal aspect ratio for square cells
ax.set_aspect('equal')

# Save
plt.tight_layout()
plt.savefig('temperature_field.png', dpi=150, bbox_inches='tight')
plt.close()

print("Plot saved to: temperature_field.png")
```

## Step 4: Enhance Visualization

Add contour lines and improve aesthetics:

```python
import h5py
import matplotlib.pyplot as plt
import numpy as np

# Read data
with h5py.File('simulation_output.h5', 'r') as f:
    temperature = f['/results/temperature'][:]
    x = f['/coordinates/x'][:]
    y = f['/coordinates/y'][:]
    units = f['/results/temperature'].attrs['units']
    time = f['/results/temperature'].attrs['time']

# Create plot
fig, ax = plt.subplots(figsize=(12, 9))

# Plot temperature field
im = ax.pcolormesh(x, y, temperature, cmap='coolwarm', shading='auto')

# Add contour lines
levels = np.linspace(temperature.min(), temperature.max(), 10)
cs = ax.contour(x, y, temperature, levels=levels, colors='black',
                linewidths=0.5, alpha=0.4)
ax.clabel(cs, inline=True, fontsize=8, fmt='%.1f')

# Add colorbar
cbar = plt.colorbar(im, ax=ax)
cbar.set_label(f'Temperature ({units})', fontsize=12)

# Labels and title
ax.set_xlabel('X Position (m)', fontsize=12)
ax.set_ylabel('Y Position (m)', fontsize=12)
ax.set_title(f'Temperature Field at t={time}s', fontsize=14, fontweight='bold')

# Equal aspect ratio
ax.set_aspect('equal')

# Add grid
ax.grid(True, alpha=0.2, linestyle='--')

# Save high-quality version
plt.tight_layout()
plt.savefig('temperature_field_enhanced.png', dpi=300, bbox_inches='tight')
plt.close()

print("Enhanced plot saved to: temperature_field_enhanced.png")
```

## Step 5: Create Multiple Views

Compare temperature with pressure:

```python
import h5py
import matplotlib.pyplot as plt

# Read data
with h5py.File('simulation_output.h5', 'r') as f:
    temperature = f['/results/temperature'][:]
    pressure = f['/results/pressure'][:]
    x = f['/coordinates/x'][:]
    y = f['/coordinates/y'][:]
    time = f['/results/temperature'].attrs['time']

# Create side-by-side comparison
fig, axes = plt.subplots(1, 2, figsize=(16, 7))

# Temperature panel
im1 = axes[0].pcolormesh(x, y, temperature, cmap='coolwarm', shading='auto')
axes[0].set_xlabel('X Position (m)')
axes[0].set_ylabel('Y Position (m)')
axes[0].set_title('Temperature (K)')
axes[0].set_aspect('equal')
cbar1 = plt.colorbar(im1, ax=axes[0])

# Pressure panel
im2 = axes[1].pcolormesh(x, y, pressure, cmap='viridis', shading='auto')
axes[1].set_xlabel('X Position (m)')
axes[1].set_ylabel('Y Position (m)')
axes[1].set_title('Pressure (Pa)')
axes[1].set_aspect('equal')
cbar2 = plt.colorbar(im2, ax=axes[1])

# Overall title
fig.suptitle(f'Simulation Results at t={time}s', fontsize=16, fontweight='bold')

# Save
plt.tight_layout()
plt.savefig('comparison_view.png', dpi=150, bbox_inches='tight')
plt.close()

print("Comparison plot saved to: comparison_view.png")
```

## Handling Large Datasets

If the dataset is too large to load entirely:

```python
import h5py
import matplotlib.pyplot as plt
import numpy as np

with h5py.File('large_simulation.h5', 'r') as f:
    temp_dset = f['/results/temperature']

    # Check size
    size_mb = np.prod(temp_dset.shape) * temp_dset.dtype.itemsize / (1024**2)
    print(f"Dataset size: {size_mb:.1f} MB")

    if size_mb > 100:
        print("Large dataset - applying downsampling")
        # Downsample by factor of 4
        temperature = temp_dset[::4, ::4]
        x = f['/coordinates/x'][::4]
        y = f['/coordinates/y'][::4]
    else:
        temperature = temp_dset[:]
        x = f['/coordinates/x'][:]
        y = f['/coordinates/y'][:]

# Create plot with downsampled data
fig, ax = plt.subplots(figsize=(10, 8))
im = ax.pcolormesh(x, y, temperature, cmap='coolwarm')
plt.colorbar(im, ax=ax, label='Temperature (K)')
ax.set_xlabel('X Position (m)')
ax.set_ylabel('Y Position (m)')
ax.set_title('Temperature Field (Downsampled)')

plt.savefig('temperature_downsampled.png', dpi=150)
plt.close()
```

## Complete Reusable Script

Save this as `visualize_hdf5_field.py`:

```python
#!/usr/bin/env python3
"""
Visualize 2D field from HDF5 file.

Usage:
    python visualize_hdf5_field.py <file.h5> <dataset_path> [--output <output.png>]
"""

import argparse
import h5py
import matplotlib.pyplot as plt
import numpy as np


def visualize_2d_field(filepath, dataset_path, output_path=None):
    """Visualize a 2D field from HDF5 file."""

    with h5py.File(filepath, 'r') as f:
        if dataset_path not in f:
            raise ValueError(f"Dataset {dataset_path} not found in file")

        dset = f[dataset_path]

        if len(dset.shape) != 2:
            raise ValueError(f"Dataset must be 2D, got shape {dset.shape}")

        # Load data
        data = dset[:]

        # Get metadata
        units = dset.attrs.get('units', 'unknown')
        description = dset.attrs.get('description', dataset_path)

        # Create plot
        fig, ax = plt.subplots(figsize=(10, 8))

        # Choose colormap based on name
        if 'temp' in dataset_path.lower():
            cmap = 'coolwarm'
        elif 'pressure' in dataset_path.lower():
            cmap = 'viridis'
        else:
            cmap = 'viridis'

        im = ax.pcolormesh(data, cmap=cmap, shading='auto')

        # Colorbar
        cbar = plt.colorbar(im, ax=ax)
        cbar.set_label(f'{description} ({units})', fontsize=12)

        # Labels
        ax.set_xlabel('X Index', fontsize=12)
        ax.set_ylabel('Y Index', fontsize=12)
        ax.set_title(description, fontsize=14)

        # Save
        if output_path is None:
            output_path = dataset_path.replace('/', '_').strip('_') + '.png'

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()

        print(f"Plot saved to: {output_path}")
        print(f"Shape: {data.shape}")
        print(f"Range: [{np.min(data):.2f}, {np.max(data):.2f}]")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Visualize 2D HDF5 dataset')
    parser.add_argument('filepath', help='Path to HDF5 file')
    parser.add_argument('dataset', help='Path to dataset in file')
    parser.add_argument('--output', '-o', help='Output image path')

    args = parser.parse_args()

    visualize_2d_field(args.filepath, args.dataset, args.output)
```

## Key Takeaways

1. **Always analyze first**: Check shape, size, attributes before plotting
2. **Choose appropriate plot type**: Match to data dimensionality and semantics
3. **Handle memory**: Downsample large datasets
4. **Use metadata**: Let attributes inform colormap and labels
5. **Iterate**: Start simple, enhance as needed
6. **Make it reusable**: Create scripts for repeated tasks
