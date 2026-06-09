#!/usr/bin/env python3
"""
Multi-panel visualization example for HDF5 data.

This script demonstrates creating a comprehensive visualization with:
- Main 2D field view
- Cross-section profiles
- Histogram
- Statistics panel

Useful for exploring simulation or measurement data.
"""

import h5py
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.gridspec import GridSpec


def create_comprehensive_view(filepath, dataset_path, output_path='comprehensive_view.png'):
    """
    Create a multi-panel comprehensive view of 2D HDF5 data.

    Args:
        filepath: Path to HDF5 file
        dataset_path: Path to 2D dataset within file
        output_path: Output image path
    """

    # Read data
    with h5py.File(filepath, 'r') as f:
        if dataset_path not in f:
            raise ValueError(f"Dataset {dataset_path} not found")

        dset = f[dataset_path]

        if len(dset.shape) != 2:
            raise ValueError(f"Expected 2D data, got shape {dset.shape}")

        data = dset[:]

        # Get metadata
        units = dset.attrs.get('units', '')
        description = dset.attrs.get('description', dataset_path)

    # Create figure with custom layout
    fig = plt.figure(figsize=(16, 12))
    gs = GridSpec(3, 3, figure=fig, hspace=0.3, wspace=0.4)

    # Main 2D field (top-left, 2x2 space)
    ax_main = fig.add_subplot(gs[0:2, 0:2])

    # X cross-section (bottom-left, below main plot)
    ax_x_profile = fig.add_subplot(gs[2, 0:2], sharex=ax_main)

    # Y cross-section (top-right, beside main plot)
    ax_y_profile = fig.add_subplot(gs[0:2, 2], sharey=ax_main)

    # Histogram (bottom-right)
    ax_hist = fig.add_subplot(gs[2, 2])

    # ========== Main 2D Field ==========
    im = ax_main.pcolormesh(data, cmap='viridis', shading='auto')
    ax_main.set_xlabel('X Index', fontsize=11)
    ax_main.set_ylabel('Y Index', fontsize=11)
    ax_main.set_title(f'2D Field: {description}', fontsize=13, fontweight='bold')

    # Add colorbar
    cbar = plt.colorbar(im, ax=ax_main)
    cbar.set_label(f'Value ({units})' if units else 'Value', fontsize=10)

    # Mark cross-section lines
    mid_y = data.shape[0] // 2
    mid_x = data.shape[1] // 2
    ax_main.axhline(mid_y, color='red', linestyle='--', alpha=0.5, linewidth=1)
    ax_main.axvline(mid_x, color='blue', linestyle='--', alpha=0.5, linewidth=1)

    # ========== X Cross-Section ==========
    x_profile = data[mid_y, :]
    ax_x_profile.plot(x_profile, 'r-', linewidth=1.5, label=f'Y={mid_y}')
    ax_x_profile.set_xlabel('X Index', fontsize=11)
    ax_x_profile.set_ylabel('Value', fontsize=11)
    ax_x_profile.grid(True, alpha=0.3)
    ax_x_profile.legend(fontsize=9)
    ax_x_profile.set_title('X Cross-Section', fontsize=11)

    # ========== Y Cross-Section ==========
    y_profile = data[:, mid_x]
    ax_y_profile.plot(y_profile, np.arange(len(y_profile)), 'b-', linewidth=1.5, label=f'X={mid_x}')
    ax_y_profile.set_ylabel('Y Index', fontsize=11)
    ax_y_profile.set_xlabel('Value', fontsize=11)
    ax_y_profile.grid(True, alpha=0.3)
    ax_y_profile.legend(fontsize=9)
    ax_y_profile.set_title('Y Cross-Section', fontsize=11)

    # ========== Histogram ==========
    ax_hist.hist(data.flatten(), bins=50, edgecolor='black', alpha=0.7, color='green')
    ax_hist.set_xlabel('Value', fontsize=11)
    ax_hist.set_ylabel('Frequency', fontsize=11)
    ax_hist.set_title('Distribution', fontsize=11)
    ax_hist.grid(True, alpha=0.3, axis='y')

    # Add statistics as text
    mean_val = np.mean(data)
    std_val = np.std(data)
    min_val = np.min(data)
    max_val = np.max(data)

    stats_text = f'Mean: {mean_val:.3f}\n'
    stats_text += f'Std: {std_val:.3f}\n'
    stats_text += f'Min: {min_val:.3f}\n'
    stats_text += f'Max: {max_val:.3f}'

    ax_hist.text(0.98, 0.97, stats_text,
                 transform=ax_hist.transAxes,
                 fontsize=9,
                 verticalalignment='top',
                 horizontalalignment='right',
                 bbox={'boxstyle': 'round', 'facecolor': 'wheat', 'alpha': 0.5})

    # Overall title
    fig.suptitle(f'Comprehensive View: {description}',
                 fontsize=15, fontweight='bold')

    # Save
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()

    print(f"Comprehensive view saved to: {output_path}")
    print("\nDataset Statistics:")
    print(f"  Shape: {data.shape}")
    print(f"  Mean: {mean_val:.3f}")
    print(f"  Std: {std_val:.3f}")
    print(f"  Range: [{min_val:.3f}, {max_val:.3f}]")


def create_comparison_view(filepath, dataset_paths, titles=None, output_path='comparison.png'):
    """
    Create side-by-side comparison of multiple 2D datasets.

    Args:
        filepath: Path to HDF5 file
        dataset_paths: List of dataset paths to compare
        titles: Optional list of titles for each dataset
        output_path: Output image path
    """

    n_datasets = len(dataset_paths)
    if titles is None:
        titles = dataset_paths

    # Determine grid layout
    ncols = min(3, n_datasets)
    nrows = (n_datasets + ncols - 1) // ncols

    fig, axes = plt.subplots(nrows, ncols, figsize=(5*ncols, 4*nrows))

    # Handle single subplot case
    if n_datasets == 1:
        axes = np.array([axes])

    axes = axes.flatten()

    # Read and plot each dataset
    with h5py.File(filepath, 'r') as f:
        for idx, (dpath, title) in enumerate(zip(dataset_paths, titles, strict=False)):
            if dpath not in f:
                print(f"Warning: {dpath} not found, skipping")
                continue

            data = f[dpath][:]
            units = f[dpath].attrs.get('units', '')

            ax = axes[idx]
            im = ax.pcolormesh(data, cmap='viridis', shading='auto')
            ax.set_title(title, fontsize=12)
            ax.set_xlabel('X Index')
            ax.set_ylabel('Y Index')

            cbar = plt.colorbar(im, ax=ax)
            if units:
                cbar.set_label(units, fontsize=10)

    # Hide unused subplots
    for idx in range(n_datasets, len(axes)):
        axes[idx].set_visible(False)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()

    print(f"Comparison view saved to: {output_path}")


# Example usage
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Create multi-panel HDF5 visualizations')
    parser.add_argument('filepath', help='Path to HDF5 file')
    parser.add_argument('dataset', help='Path to dataset in file')
    parser.add_argument('--output', '-o', default='comprehensive_view.png',
                        help='Output image path')
    parser.add_argument('--mode', choices=['comprehensive', 'simple'],
                        default='comprehensive',
                        help='Visualization mode')

    args = parser.parse_args()

    if args.mode == 'comprehensive':
        create_comprehensive_view(args.filepath, args.dataset, args.output)
    else:
        # Simple mode - just the 2D field
        with h5py.File(args.filepath, 'r') as f:
            data = f[args.dataset][:]
            units = f[args.dataset].attrs.get('units', '')
            description = f[args.dataset].attrs.get('description', args.dataset)

        fig, ax = plt.subplots(figsize=(10, 8))
        im = ax.pcolormesh(data, cmap='viridis', shading='auto')
        plt.colorbar(im, ax=ax, label=units)
        ax.set_xlabel('X Index')
        ax.set_ylabel('Y Index')
        ax.set_title(description)
        plt.tight_layout()
        plt.savefig(args.output, dpi=150, bbox_inches='tight')
        plt.close()

        print(f"Simple view saved to: {args.output}")
