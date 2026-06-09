# DOI Resolution and Data Retrieval

How to programmatically resolve DOIs and citations from scientific papers to downloadable HDF5 files.

## DOI Resolution Chain

The typical chain: DOI string → doi.org resolver → landing page → download link.

### Content Negotiation (Most Powerful Method)

DOI.org supports content negotiation — request metadata in a specific format instead of getting a landing page redirect:

```python
import requests

doi = '10.5281/zenodo.123456'

# Get DataCite JSON metadata
r = requests.get(f'https://doi.org/{doi}',
    headers={'Accept': 'application/vnd.datacite.datacite+json'},
    allow_redirects=True)
metadata = r.json()

# Get Citeproc JSON (citation metadata)
r = requests.get(f'https://doi.org/{doi}',
    headers={'Accept': 'application/vnd.citationstyles.csl+json'})

# Get BibTeX
r = requests.get(f'https://doi.org/{doi}',
    headers={'Accept': 'application/x-bibtex'})

# Get RDF/XML
r = requests.get(f'https://doi.org/{doi}',
    headers={'Accept': 'application/rdf+xml'})
```

Supported by CrossRef, DataCite, and mEDRA registration agencies.

### DOI REST API

Direct resolution without content negotiation:

```python
# Get DOI metadata and resolution target
r = requests.get(f'https://doi.org/api/handles/{doi}')
handle_data = r.json()
# Returns handle record with URL values
```

## DataCite API

For dataset DOIs (Zenodo, Figshare, Dryad, Dataverse, etc.), DataCite is the registration agency.

### Retrieve Single DOI

```python
r = requests.get(f'https://api.datacite.org/dois/{doi}')
data = r.json()['data']['attributes']

# Key fields:
url = data['url']                    # Landing page URL
titles = data['titles']              # [{title: "..."}]
creators = data['creators']          # [{name: "...", affiliation: [...]}]
dates = data['dates']                # [{date: "2024", dateType: "Issued"}]
types = data['types']                # {resourceTypeGeneral: "Dataset"}
related = data['relatedIdentifiers'] # Links to papers, other datasets
formats = data['formats']            # ["application/x-hdf5"] if specified
sizes = data['sizes']                # File sizes
rights = data['rightsList']          # Licenses
container = data['container']        # Journal/repo info
```

### Search DataCite

```python
# Search for HDF5 datasets
r = requests.get('https://api.datacite.org/dois', params={
    'query': 'HDF5 sea surface temperature',
    'resource-type-id': 'dataset',
    'page[size]': 25
})

# Search by creator
r = requests.get('https://api.datacite.org/dois', params={
    'query': 'creators.name:Smith',
    'resource-type-id': 'dataset'
})
```

### Find Related Papers from Dataset DOI

```python
r = requests.get(f'https://api.datacite.org/dois/{doi}')
related = r.json()['data']['attributes']['relatedIdentifiers']
papers = [ri for ri in related
          if ri.get('relationType') in ('IsSupplementTo', 'IsReferencedBy', 'IsDocumentedBy')
          and ri.get('relatedIdentifierType') == 'DOI']
```

## CrossRef API

For paper DOIs (journal articles). Useful for finding datasets referenced by papers.

### Get Paper Metadata

```python
r = requests.get(f'https://api.crossref.org/works/{paper_doi}',
    headers={'User-Agent': 'MyApp/1.0 (mailto:user@example.com)'})
paper = r.json()['message']

# Check for linked datasets
references = paper.get('reference', [])
relations = paper.get('relation', {})
# 'relation' may contain 'has-related-dataset', 'is-supplemented-by', etc.
```

### Citation Text → DOI

CrossRef provides a text query API for parsing citation strings into DOIs:

```python
# Parse citation string to DOI
citation = "Smith et al. (2024) Global SST observations. Scientific Data, 11, 234."
r = requests.get('https://api.crossref.org/works',
    params={'query.bibliographic': citation, 'rows': 3})
results = r.json()['message']['items']
best_match = results[0]  # Sorted by relevance
doi = best_match['DOI']
score = best_match['score']
```

**Reliability**: CrossRef text query is best-effort. Works well for well-formed citations but may fail for informal references or preprints.

### Bulk Metadata

CrossRef's 2025 public data file contains metadata for all 165M+ registered DOIs:
`https://doi.org/10.13003/87bfgcee6g`

## Repository-Specific Resolution

### Zenodo DOI → Files

```python
# Zenodo DOIs follow pattern: 10.5281/zenodo.{record_id}
record_id = doi.split('zenodo.')[-1]

# Get record with file list
r = requests.get(f'https://zenodo.org/api/records/{record_id}')
record = r.json()

# Find HDF5 files
hdf5_files = [f for f in record['files']
              if f['key'].endswith(('.h5', '.hdf5', '.hdf', '.he5'))]

for f in hdf5_files:
    print(f"File: {f['key']}, Size: {f['size']}, MD5: {f['checksum']}")
    download_url = f['links']['self']
    # Download: requests.get(download_url, stream=True)
```

**Concept DOI**: If the DOI is a concept DOI (resolves to latest version), the API returns a 302 redirect to the latest version record.

### Zenodo Search by Keywords

```python
r = requests.get('https://zenodo.org/api/records', params={
    'q': 'keywords:"HDF5" AND keywords:"sea surface temperature"',
    'type': 'dataset',
    'sort': 'bestmatch',
    'size': 10
})
```

### NASA Earthdata DOI → Granules

NASA DOIs (prefix `10.5067`) resolve to collection landing pages. To get actual HDF5 files:

```python
import earthaccess

earthaccess.login()

# Method 1: Search by DOI
results = earthaccess.search_data(
    doi='10.5067/MODIS/MOD04_L2.006',
    temporal=('2024-01-01', '2024-01-31')
)

# Method 2: Search by short name
results = earthaccess.search_data(
    short_name='MOD04_L2',
    version='6.1',
    temporal=('2024-01-01', '2024-01-31'),
    bounding_box=(-180, -90, 180, 90)
)

# Download
files = earthaccess.download(results, './data/')

# Or use CMR directly
import requests
r = requests.get('https://cmr.earthdata.nasa.gov/search/collections.json',
    params={'doi[]': '10.5067/MODIS/MOD04_L2.006'})
concept_id = r.json()['feed']['entry'][0]['id']

r = requests.get('https://cmr.earthdata.nasa.gov/search/granules.json',
    params={
        'collection_concept_id': concept_id,
        'temporal[]': '2024-01-01T00:00:00Z,2024-01-31T23:59:59Z',
        'page_size': 100
    })
```

### Figshare DOI → Files

```python
# Figshare DOIs: resolve landing page, extract article ID
# Or use API directly with article ID
r = requests.get(f'https://api.figshare.com/v2/articles/{article_id}')
files = r.json()['files']
for f in files:
    if f['name'].endswith(('.h5', '.hdf5', '.hdf')):
        download_url = f['download_url']
```

### Dataverse DOI → Files

```python
# Get dataset by persistent ID
r = requests.get(f'{SERVER}/api/datasets/:persistentId',
    params={'persistentId': f'doi:{doi}'},
    headers={'X-Dataverse-key': TOKEN})
files = r.json()['data']['latestVersion']['files']
for f in files:
    file_id = f['dataFile']['id']
    download_url = f'{SERVER}/api/access/datafile/{file_id}'
```

## Sub-File Addressing (No Standard)

There is no standard for encoding HDF5 internal dataset paths in DOIs or URLs.

### Current State

- **HSDS REST API** uses URL paths: `https://server/datasets?domain=/path/to/file.h5&h5path=/group/dataset`
- **OPeNDAP** uses URL path syntax: `https://opendap.server/path/file.h5.dap?dap4.ce=/variable`
- **RDA Dynamic Data Citation** proposes timestamped queries for subsetting, but this is not widely implemented
- **No fragment identifier** standard (like `#/group/dataset` appended to DOI)

### Practical Approach for Agents

1. Parse paper text for variable names, dataset descriptions, or HDF5 paths
2. Download the full HDF5 file
3. Explore structure with `h5py` to locate referenced data:

```python
import h5py

with h5py.File('downloaded.h5', 'r') as f:
    # Walk all datasets
    datasets = {}
    def collect(name, obj):
        if isinstance(obj, h5py.Dataset):
            datasets[name] = {
                'shape': obj.shape,
                'dtype': str(obj.dtype),
                'attrs': dict(obj.attrs)
            }
    f.visititems(collect)

    # Search by standard_name attribute
    for path, info in datasets.items():
        if info['attrs'].get('standard_name') == 'sea_surface_temperature':
            print(f"Found: {path}")
```
