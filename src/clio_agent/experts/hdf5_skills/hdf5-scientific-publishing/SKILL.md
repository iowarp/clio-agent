---
name: hdf5-scientific-publishing
description: "HDF5 DOI", "cite HDF5 data", "publish HDF5 to Zenodo", "upload to Dataverse", "FAIR HDF5", "reproducible HDF5", "find HDF5 dataset from paper", "download data from DOI", "HDF5 metadata for publication", "scientific data repository", depositing/retrieving HDF5 from Zenodo/Figshare/Dataverse/Earthdata, FAIR-compliant HDF5.
version: 0.1.0
---

# HDF5 for Scientific Publishing and Reproducibility

## Purpose

Guidance for making HDF5 data FAIR-compliant, depositing it in scientific repositories with proper metadata, and retrieving HDF5 data referenced in scientific papers. Covers the full lifecycle: create (with metadata) -> deposit (with DOI) -> cite -> retrieve -> reproduce.

**Related skills**: `hdf5-cloud-optimized` for remote access optimization, `hdf5-filters` for compression before upload.

## Key Concepts

### No Standard for Sub-File DOI Addressing

There is no formal standard for mapping a DOI to a specific dataset *within* an HDF5 file. DOIs point to files or collections, not internal paths like `/temperature/surface`. To reference internal datasets, the agent must parse paper text for variable/dataset names and navigate the HDF5 structure after download.

The HSDS REST API supports URL-path-based addressing of internal datasets, but this is a service protocol, not a persistent identifier standard.

### DOI Granularity Varies by Repository

- **NASA Earthdata**: DOIs are **collection-level** (all granules share one DOI, e.g., `10.5067/MODIS/MOD04_L2.006`). Individual files identified by `GranuleUR` or `concept_id`.
- **Zenodo**: One DOI per **deposit** (not per file). Concept DOI (all versions) + Version DOI (specific version).
- **Figshare**: DOI per **item** at publication time.
- **Dataverse**: DOI per **dataset** (which can contain multiple files).

### Metadata Convention Stack

Three layered conventions apply to HDF5 files for scientific publishing:
1. **CF Conventions** (Climate & Forecast): `title`, `history`, `source`, `references`, `institution`, `comment`
2. **ACDD 1.3** (Attribute Convention for Data Discovery): Extends CF with `id`, `naming_authority`, `creator_name`, `license`, `keywords`, `date_created`
3. **h5rdmtoolbox** (Python): Maps HDF5 attributes to RDF triples with IRIs for full FAIR/semantic compliance

## FAIR Minimum Attributes for HDF5

Embed these root-level attributes before depositing any HDF5 file:

| Attribute | Purpose | Example |
|-----------|---------|---------|
| `Conventions` | Declare standards | `"CF-1.8, ACDD-1.3"` |
| `id` | Globally unique ID | `"10.5281/zenodo.123456"` |
| `naming_authority` | ID namespace | `"org.zenodo"` |
| `title` | Human-readable title | `"Global SST Observations 2024"` |
| `summary` | Description | Free text |
| `keywords` | Searchable terms | `"sea surface temperature, SST"` |
| `creator_name` | Author | `"Smith, Jane"` |
| `institution` | Affiliation | `"NASA JPL"` |
| `date_created` | ISO 8601 timestamp | `"2024-01-15T00:00:00Z"` |
| `license` | SPDX identifier | `"CC-BY-4.0"` |
| `source` | Data provenance | `"MODIS Aqua L2 v6.1"` |
| `history` | Processing audit trail | `"2024-01-15: Created by process.py v2.3"` |
| `references` | Related publications | `"https://doi.org/10.1234/paper"` |

Per-variable: add `units` and `standard_name` (from CF standard name table) for interoperability.

```python
import h5py

with h5py.File('output.h5', 'w') as f:
    f.attrs['Conventions'] = 'CF-1.8, ACDD-1.3'
    f.attrs['title'] = 'Global SST Observations 2024'
    f.attrs['id'] = '10.5281/zenodo.123456'
    f.attrs['naming_authority'] = 'org.zenodo'
    f.attrs['creator_name'] = 'Smith, Jane'
    f.attrs['institution'] = 'NASA JPL'
    f.attrs['date_created'] = '2024-01-15T00:00:00Z'
    f.attrs['license'] = 'CC-BY-4.0'
    f.attrs['source'] = 'MODIS Aqua L2 v6.1'
    f.attrs['history'] = '2024-01-15: Created by process.py v2.3'
    f.attrs['references'] = 'https://doi.org/10.1234/paper'
    f.attrs['summary'] = 'Daily sea surface temperature observations...'
    f.attrs['keywords'] = 'sea surface temperature, SST, ocean, remote sensing'

    dset = f.create_dataset('sst', data=sst_data, chunks=True, compression='gzip')
    dset.attrs['units'] = 'K'
    dset.attrs['standard_name'] = 'sea_surface_temperature'
    dset.attrs['long_name'] = 'Sea Surface Temperature'
```

## Workflow: Deposit HDF5 to Repository

### Repository Selection

| Repository | Best For | HDF5 Support | Size Limit |
|-----------|----------|--------------|------------|
| **Zenodo** | General scientific data, quick DOI | Opaque blob (use h5rdmtoolbox for JSON-LD sidecar) | 50 GB / 100 files |
| **Dataverse** | Structured research data | **Active**: NcML extraction, H5Web previewer, geospatial bbox | ~2.5 GB (configurable) |
| **Figshare** | Any research output | Opaque blob | 20 GB (web), 5 TB (FTP) |
| **Dryad** | Data underlying publications | Opaque blob, prefers open formats | 10 GB/file, 2 TB total |
| **NASA Earthdata** | NASA-funded earth science | Full HDF-EOS5 support | Not open for general deposit |

### Zenodo Deposit (Most Common Path)

```python
import requests

TOKEN = 'your_access_token'
BASE = 'https://zenodo.org/api'
HEADERS = {'Authorization': f'Bearer {TOKEN}'}

# 1. Create empty deposit
r = requests.post(f'{BASE}/deposit/depositions', json={}, headers=HEADERS)
dep_id = r.json()['id']
bucket_url = r.json()['links']['bucket']

# 2. Upload HDF5 file
with open('data.h5', 'rb') as fp:
    r = requests.put(f'{bucket_url}/data.h5', data=fp, headers=HEADERS)

# 3. Set metadata (link to paper via related_identifiers)
metadata = {
    'metadata': {
        'title': 'Global SST Observations 2024',
        'upload_type': 'dataset',
        'description': 'Daily sea surface temperature observations...',
        'creators': [{'name': 'Smith, Jane', 'affiliation': 'NASA JPL', 'orcid': '0000-0001-2345-6789'}],
        'keywords': ['HDF5', 'sea surface temperature', 'SST'],
        'license': 'cc-by-4.0',
        'related_identifiers': [
            {'identifier': '10.1234/paper', 'relation': 'isSupplementTo', 'scheme': 'doi'}
        ]
    }
}
r = requests.put(f'{BASE}/deposit/depositions/{dep_id}', json=metadata, headers=HEADERS)

# 4. Publish (mints DOI)
r = requests.post(f'{BASE}/deposit/depositions/{dep_id}/actions/publish', headers=HEADERS)
doi = r.json()['doi']
```

Use `https://sandbox.zenodo.org/api` for testing. See `references/repository-apis.md` for Figshare, Dataverse, and Earthdata API details.

## Workflow: Retrieve HDF5 from DOI

### Step 1: Resolve DOI to Metadata

```python
import requests

doi = '10.5281/zenodo.123456'

# Option A: DataCite API (works for all DataCite DOIs: Zenodo, Figshare, Dryad, etc.)
r = requests.get(f'https://api.datacite.org/dois/{doi}')
metadata = r.json()['data']['attributes']
landing_url = metadata['url']

# Option B: DOI content negotiation
r = requests.get(f'https://doi.org/{doi}',
    headers={'Accept': 'application/vnd.datacite.datacite+json'})

# Option C: CrossRef API (for paper DOIs - check for related datasets)
r = requests.get(f'https://api.crossref.org/works/{doi}')
```

### Step 2: Download from Repository

**Zenodo**: Extract record ID from DOI, fetch file list.
```python
record_id = doi.split('zenodo.')[-1]
r = requests.get(f'https://zenodo.org/api/records/{record_id}')
files = r.json()['files']
for f in files:
    if f['key'].endswith(('.h5', '.hdf5', '.hdf')):
        download_url = f['links']['self']
        # Download file
```

**NASA Earthdata**: Search CMR by DOI, then download granules.
```python
import earthaccess
earthaccess.login()
results = earthaccess.search_data(doi='10.5067/MODIS/MOD04_L2.006',
                                   temporal=('2024-01-01', '2024-01-31'),
                                   bounding_box=(-180, -90, 180, 90))
earthaccess.download(results, './data/')
```

See `references/doi-resolution.md` for complete resolution chains and citation parsing.

### Step 3: Navigate Internal Structure

After downloading, open the file and locate referenced datasets:
```python
import h5py

with h5py.File('downloaded.h5', 'r') as f:
    # List all datasets
    def visitor(name, obj):
        if isinstance(obj, h5py.Dataset):
            print(f'{name}: shape={obj.shape}, dtype={obj.dtype}')
    f.visititems(visitor)

    # Access specific dataset referenced in paper
    sst = f['/ocean/sea_surface_temperature'][:]
```

## HDF Group Citation Guidance

When citing HDF5 software in publications, include a DataCite `relatedIdentifier`:
- `relatedIdentifierType="DOI"`, `relationType="Requires"`, value `10.11578/dc.20180330.1`

## Validation Tools

- **cf-checker** (`pip install cfchecker`): Validate CF Convention compliance
- **h5rdmtoolbox** (`pip install h5rdmtoolbox`): FAIR metadata with RDF/IRI support, auto-generates JSON-LD sidecar for Zenodo
- **ACDD checker**: Part of IOOS compliance checker (`pip install compliance-checker`)

## Additional Resources

### Reference Files

For detailed API specifications and metadata standards, consult:
- **`references/repository-apis.md`** - Complete API workflows for Zenodo, Figshare, Dataverse, NASA Earthdata (endpoints, auth, metadata fields, size limits)
- **`references/doi-resolution.md`** - DOI resolution chain details, DataCite/CrossRef APIs, citation parsing tools, NASA CMR search
- **`references/metadata-standards.md`** - Full CF Convention attribute list, ACDD 1.3 attributes, HDF-EOS5 structure, h5rdmtoolbox RDF/IRI examples, DataCite schema mapping
