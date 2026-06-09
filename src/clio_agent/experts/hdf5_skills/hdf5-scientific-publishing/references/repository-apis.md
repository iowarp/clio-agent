# Repository API Reference

Complete API workflows for depositing and retrieving HDF5 files from scientific data repositories.

## Zenodo REST API

### Authentication

All requests require HTTPS + OAuth 2.0 bearer token.

Create a personal access token at https://zenodo.org/account/settings/applications/ with scopes:
- `deposit:write` - Create and edit deposits
- `deposit:actions` - Publish deposits

```
Authorization: Bearer <ACCESS_TOKEN>
```

### Endpoints

| Action | Method | Endpoint |
|--------|--------|----------|
| Create deposit | POST | `/api/deposit/depositions` |
| Upload file | PUT | `{bucket_url}/{filename}` |
| Update metadata | PUT | `/api/deposit/depositions/{id}` |
| Publish | POST | `/api/deposit/depositions/{id}/actions/publish` |
| Search records | GET | `/api/records?q={query}` |
| Get record | GET | `/api/records/{id}` |
| New version | POST | `/api/deposit/depositions/{id}/actions/newversion` |

Base URL: `https://zenodo.org/api` (production), `https://sandbox.zenodo.org/api` (testing)

### Complete Deposit Workflow

```python
import requests
import json

TOKEN = 'your_access_token'
BASE = 'https://zenodo.org/api'
H = {'Authorization': f'Bearer {TOKEN}'}

# Step 1: Create empty deposit
r = requests.post(f'{BASE}/deposit/depositions', json={}, headers=H)
dep = r.json()
dep_id = dep['id']
bucket = dep['links']['bucket']

# Step 2: Upload file(s) to bucket
with open('data.h5', 'rb') as fp:
    r = requests.put(f'{bucket}/data.h5', data=fp, headers=H)

# Step 3: Set metadata
metadata = {
    'metadata': {
        'title': 'Dataset Title',
        'upload_type': 'dataset',
        'description': '<p>Dataset description with <b>HTML</b> allowed.</p>',
        'creators': [
            {
                'name': 'Last, First',
                'affiliation': 'Institution',
                'orcid': '0000-0001-2345-6789'  # optional
            }
        ],
        'keywords': ['HDF5', 'keyword1', 'keyword2'],
        'license': 'cc-by-4.0',
        'access_right': 'open',  # open|embargoed|restricted|closed
        'related_identifiers': [
            {
                'identifier': '10.1234/paper.doi',
                'relation': 'isSupplementTo',
                'scheme': 'doi'
            }
        ],
        'notes': 'Additional notes',
        'communities': [{'identifier': 'community-id'}]  # optional
    }
}
r = requests.put(f'{BASE}/deposit/depositions/{dep_id}', json=metadata, headers=H)

# Step 4: Publish
r = requests.post(f'{BASE}/deposit/depositions/{dep_id}/actions/publish', headers=H)
result = r.json()
print(f"DOI: {result['doi']}")
print(f"URL: {result['links']['record_html']}")
```

### Metadata Fields

**Required**: `title`, `upload_type`, `description`, `creators`

**upload_type values**: `publication`, `poster`, `presentation`, `dataset`, `image`, `video`, `software`, `lesson`, `physicalobject`, `other`

**access_right values**: `open` (default), `embargoed`, `restricted`, `closed`

**related_identifiers relation types**: `isCitedBy`, `cites`, `isSupplementTo`, `isSupplementedBy`, `isContinuedBy`, `continues`, `isDescribedBy`, `describes`, `hasMetadata`, `isMetadataFor`, `isNewVersionOf`, `isPreviousVersionOf`, `isPartOf`, `hasPart`, `isReferencedBy`, `references`, `isDocumentedBy`, `documents`, `isCompiledBy`, `compiles`, `isVariantFormOf`, `isOriginalFormOf`, `isIdenticalTo`, `isAlternateIdentifier`, `isDerivedFrom`, `isSourceOf`, `isRequiredBy`, `requires`, `isObsoletedBy`, `obsoletes`

**identifier scheme values**: `doi`, `handle`, `ark`, `purl`, `issn`, `isbn`, `pubmed`, `pmcid`, `ads`, `arxiv`, `lsid`, `ean13`, `istc`, `urn`, `url`

### Versioning

```python
# Create new version of existing deposit
r = requests.post(f'{BASE}/deposit/depositions/{dep_id}/actions/newversion', headers=H)
new_dep_url = r.json()['links']['latest_draft']
r = requests.get(new_dep_url, headers=H)
new_dep_id = r.json()['id']
# Now upload new files and publish as before
```

### Search

```python
# Search for HDF5 datasets
r = requests.get(f'{BASE}/records', params={
    'q': 'HDF5 sea surface temperature',
    'type': 'dataset',
    'sort': 'bestmatch',
    'size': 10
})
```

Elasticsearch query syntax. Filter by `file_type` post-filter. Field search: `title:HDF5`, `creators.name:Smith`.

### Limits

- 50 GB total per deposit, 100 files max
- Rate limiting per endpoint (varies)
- DOI minted on publish, cannot un-publish

### h5rdmtoolbox Integration

When uploading HDF5 with `auto_map_hdf=True`, h5rdmtoolbox auto-generates a JSON-LD sidecar file extracting metadata from HDF5 attributes, improving discoverability.

---

## Figshare API v2

### Authentication

Personal token or OAuth2. Create at https://figshare.com/account/applications

```
Authorization: token <PERSONAL_TOKEN>
```

### Base URL

`https://api.figshare.com/v2`

### Complete Deposit Workflow

```python
import requests, hashlib, os, json

TOKEN = 'your_token'
BASE = 'https://api.figshare.com/v2'
H = {'Authorization': f'token {TOKEN}'}

# Step 1: Create article (private draft)
article_data = {
    'title': 'Dataset Title',
    'description': 'Description of dataset',
    'keywords': ['HDF5', 'keyword'],
    'categories': [123],  # Figshare category IDs
    'defined_type': 'dataset',
    'license': 1  # CC-BY = 1
}
r = requests.post(f'{BASE}/account/articles', json=article_data, headers=H)
article_id = r.json()['entity_id']

# Step 2: Initiate file upload
file_path = 'data.h5'
file_size = os.path.getsize(file_path)
with open(file_path, 'rb') as f:
    md5 = hashlib.md5(f.read()).hexdigest()
file_data = {'name': os.path.basename(file_path), 'md5': md5, 'size': file_size}
r = requests.post(f'{BASE}/account/articles/{article_id}/files', json=file_data, headers=H)
file_info = r.json()

# Step 3: Upload file parts
upload_url = file_info['location']
r = requests.get(upload_url, headers=H)
parts = r.json()['parts']
with open(file_path, 'rb') as f:
    for part in parts:
        f.seek(part['startOffset'])
        data = f.read(part['endOffset'] - part['startOffset'] + 1)
        requests.put(f"{upload_url}/{part['partNo']}", data=data, headers=H)

# Step 4: Complete upload
requests.post(upload_url, headers=H)

# Step 5: Publish
requests.post(f'{BASE}/account/articles/{article_id}/publish', headers=H)
```

### Limits

- 20 GB per file (web), 5 TB per file (FTP/API)
- 20 GB free private storage
- DOI minted per item at publication

---

## Harvard Dataverse Native API

### Authentication

API token passed as header: `X-Dataverse-key: {token}`

### HDF5-Specific Features

Dataverse is the only generalist repository with active HDF5 support:
- **NcML extraction**: Automatic metadata extraction from HDF5 on upload
- **H5Web previewer**: In-browser visualization of HDF5 contents
- **Geospatial bbox**: Auto-extracts bounding box from CF-compliant HDF5

### Complete Deposit Workflow

```bash
# Step 1: Create dataset with JSON metadata
curl -H "X-Dataverse-key: $TOKEN" \
  -X POST "$SERVER/api/dataverses/$ALIAS/datasets" \
  --upload-file dataset.json

# Step 2: Upload file
curl -H "X-Dataverse-key: $TOKEN" \
  -X POST "$SERVER/api/datasets/:persistentId/add?persistentId=$PID" \
  -F "file=@data.h5" \
  -F 'jsonData={"description":"HDF5 dataset","categories":["Data"]}'

# Step 3: Publish
curl -H "X-Dataverse-key: $TOKEN" \
  -X POST "$SERVER/api/datasets/:persistentId/actions/:publish?persistentId=$PID&type=major"
```

### SWORD API Alternative

For simpler deposits with limited metadata (Dublin Core subset):
```bash
# Service document
curl -u $TOKEN: $SERVER/dvn/api/data-deposit/v1.1/swordv2/service-document
```

### Limits

- Configurable per installation (Harvard: ~2.5 GB default)
- NcML extraction disabled for S3 direct upload by default

---

## NASA Earthdata (CMR)

### Not Open for General Deposit

NASA DAACs do not accept unsolicited data deposits. The process:

1. **NASA-funded projects**: Contact assigned DAAC. DAAC works with NASA on compliance.
2. **Non-NASA research**: Must demonstrate NASA interest. Contact DAAC (e.g., ORNL DAAC submit form).
3. **Review criteria**: Scientific impact, community need, DAAC mission alignment.

### CMR Search API (Read-Only, No Auth Required)

Base: `https://cmr.earthdata.nasa.gov/search`

```python
import requests

# Search collections by DOI
r = requests.get('https://cmr.earthdata.nasa.gov/search/collections.json',
    params={'doi[]': '10.5067/MODIS/MOD04_L2.006'})
collections = r.json()['feed']['entry']
concept_id = collections[0]['id']

# Search granules within collection
r = requests.get('https://cmr.earthdata.nasa.gov/search/granules.json',
    params={
        'collection_concept_id': concept_id,
        'temporal[]': '2024-01-01T00:00:00Z,2024-01-31T23:59:59Z',
        'bounding_box[]': '-180,-90,180,90',
        'page_size': 10
    })
granules = r.json()['feed']['entry']
```

### earthaccess Python Library

Higher-level wrapper for CMR + auth + download:

```python
import earthaccess

earthaccess.login()  # Uses .netrc or interactive

# Search
results = earthaccess.search_data(
    doi='10.5067/MODIS/MOD04_L2.006',
    temporal=('2024-01-01', '2024-01-31'),
    bounding_box=(-180, -90, 180, 90)
)

# Download
files = earthaccess.download(results, './data/')

# Or open directly (cloud or local)
datasets = earthaccess.open(results)
```

### Key CMR Search Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `doi[]` | Collection DOI | `10.5067/MODIS/MOD04_L2.006` |
| `short_name` | Product short name | `MOD04_L2` |
| `temporal[]` | Date range (ISO 8601) | `2024-01-01T00:00:00Z,2024-12-31T23:59:59Z` |
| `bounding_box[]` | Spatial bounds (W,S,E,N) | `-180,-90,180,90` |
| `granule_ur` | Specific granule ID | `MOD04_L2.A2024001...` |
| `concept_id` | CMR concept ID | `C1234567-LPDAAC_ECS` |
| `page_size` | Results per page | `100` |

Response formats: JSON (`.json`), XML, ATOM, CSV, STAC (`.stac`), UMM-JSON (`.umm_json`)

---

## Dryad API v2

### Base URL

`https://datadryad.org/api/v2`

Documentation: `https://datadryad.org/api/v2/docs/`

### Key Details

- DOI reserved at submission start, registered on publication (after 1-2 week curation)
- Metadata schema: DataCite-inspired with Dryad-specific additions
- Required fields: `title`, `authors[]` (firstName, lastName, email, affiliation), `abstract`
- 10 GB per file, 2 TB per publication
- Uses DataCite, Dublin Core, MODS, OAI-ORE metadata standards

---

## Wikidata (Linking, Not Hosting)

Wikidata does not host data files but can link to HDF5 datasets via properties:

| Property | Purpose | Example |
|----------|---------|---------|
| `P953` | Full work available at URL | Link to repository download |
| `P2701` | File format | Q61080677 (HDF5) |
| `P2702` | Dataset distribution | URL to data access |
| `P437` | Distribution format | Link to format item |
| `P356` | DOI | `10.5281/zenodo.123456` |

Useful for creating structured knowledge graph links between papers, datasets, and repositories.
