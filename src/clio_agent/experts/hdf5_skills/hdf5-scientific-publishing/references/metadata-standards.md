# Metadata Standards for HDF5 Scientific Data

Detailed reference for metadata conventions, attribute lists, and tools for embedding provenance and citation information in HDF5 files.

## CF Conventions (Climate and Forecast)

The most widely adopted metadata convention for NetCDF/HDF5 in earth sciences. Current version: CF-1.11.

### Global Attributes

| Attribute | Type | Description | Required |
|-----------|------|-------------|----------|
| `Conventions` | string | Convention name and version | Recommended |
| `title` | string | Short description of file contents | Recommended |
| `institution` | string | Where data was produced | Recommended |
| `source` | string | Method of production (model name/version, instrument type) | Recommended |
| `history` | string | Audit trail of modifications, each line starting with timestamp | Recommended |
| `references` | string | Published or web-based references describing the data | Recommended |
| `comment` | string | Miscellaneous information | Recommended |

None are technically required for backwards compatibility with COARDS, but all are strongly recommended.

### Variable-Level Attributes

| Attribute | Description | Example |
|-----------|-------------|---------|
| `standard_name` | From CF Standard Name Table | `sea_surface_temperature` |
| `long_name` | Human-readable name | `Sea Surface Temperature` |
| `units` | UDUNITS-compatible | `K`, `m s-1`, `kg m-2` |
| `calendar` | For time variables | `standard`, `gregorian`, `360_day` |
| `_FillValue` | Missing data sentinel | `-9999.0` |
| `valid_range` | Data validity bounds | `[200.0, 350.0]` |
| `cell_methods` | How data was computed | `time: mean` |
| `coordinates` | Auxiliary coordinate vars | `lat lon` |

### CF Standard Name Table

Over 4,000 standardized variable names. Search at: https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html

Key categories: atmosphere, ocean, land surface, sea ice, cloud, radiation.

---

## ACDD 1.3 (Attribute Convention for Data Discovery)

Maintained by ESIP (Earth Science Information Partners). Extends CF for discovery system mapping to Dublin Core, DIF, ISO 19115.

Full specification: https://wiki.esipfed.org/Attribute_Convention_for_Data_Discovery_1-3

### Highly Recommended Attributes

| Attribute | Description | Example |
|-----------|-------------|---------|
| `title` | Dataset title | `"Global SST Observations 2024"` |
| `summary` | Paragraph description | Free text |
| `keywords` | Comma-separated terms | `"EARTH SCIENCE, OCEANS, SST"` |
| `id` | Unique identifier (can be DOI) | `"10.5281/zenodo.123456"` |
| `naming_authority` | ID issuer | `"org.zenodo"` |
| `Conventions` | Standards followed | `"CF-1.8, ACDD-1.3"` |

### Recommended Attributes

| Attribute | Description | Example |
|-----------|-------------|---------|
| `history` | Processing audit trail | Timestamped entries |
| `source` | Production method | `"satellite observation"` |
| `processing_level` | Data processing level | `"L2"` |
| `comment` | Misc information | Free text |
| `date_created` | ISO 8601 creation date | `"2024-01-15T00:00:00Z"` |
| `date_modified` | ISO 8601 modification date | `"2024-02-01T00:00:00Z"` |
| `date_issued` | ISO 8601 publication date | `"2024-03-01"` |
| `creator_name` | Dataset author | `"Smith, Jane"` |
| `creator_email` | Author email | `"jane@example.com"` |
| `creator_url` | Author URL | `"https://orcid.org/0000-..."` |
| `creator_institution` | Author institution | `"NASA JPL"` |
| `publisher_name` | Data publisher | `"Zenodo"` |
| `publisher_email` | Publisher email | `"info@zenodo.org"` |
| `publisher_url` | Publisher URL | `"https://zenodo.org"` |
| `license` | Data license | `"CC-BY-4.0"` |
| `standard_name_vocabulary` | CF standard names version | `"CF Standard Name Table v83"` |
| `keywords_vocabulary` | Keyword source | `"GCMD Science Keywords"` |

### Suggested Attributes

| Attribute | Description |
|-----------|-------------|
| `contributor_name` | Additional contributors |
| `contributor_role` | Contributor roles |
| `program` | Overarching program |
| `project` | Specific project |
| `platform` | Observing platform |
| `instrument` | Observing instrument |
| `product_version` | Product version |
| `references` | Publication references |
| `acknowledgement` | Funding acknowledgements |

### Geospatial/Temporal Extent

| Attribute | Description |
|-----------|-------------|
| `geospatial_lat_min` | Southern bounding latitude |
| `geospatial_lat_max` | Northern bounding latitude |
| `geospatial_lon_min` | Western bounding longitude |
| `geospatial_lon_max` | Eastern bounding longitude |
| `geospatial_vertical_min` | Minimum altitude/depth |
| `geospatial_vertical_max` | Maximum altitude/depth |
| `time_coverage_start` | ISO 8601 start time |
| `time_coverage_end` | ISO 8601 end time |
| `time_coverage_duration` | ISO 8601 duration |
| `time_coverage_resolution` | ISO 8601 resolution |

---

## HDF-EOS5 (NASA Earth Observing System)

Extension of HDF5 for NASA earth science data. Adds structural metadata for geolocation.

### Internal Structure

HDF-EOS5 files contain a group called `HDFEOS INFORMATION` with global attributes:

| Attribute Family | Purpose |
|-----------------|---------|
| `StructMetadata.0` ... `.9` | Geolocation structure (Grid/Swath/Point definitions) |
| `coremetadata.0` ... `.9` | Core EOSDIS metadata (spatial/temporal info) |
| `archivemetadata.0` ... `.9` | Product-specific archival metadata |
| `productmetadata.0` ... `.9` | Product-specific operational metadata |

### Data Structures

| Type | Description | Use Case |
|------|-------------|----------|
| **Grid** | Regular gridded data | Rasterized products (MODIS, VIIRS) |
| **Swath** | Satellite swath data | Along-track measurements |
| **Point** | Point-based measurements | Station observations |
| **Zonal Average** | Zonally averaged data | Climate averages |

All structures map onto standard HDF5 groups, datasets, and attributes. Classes like `SWATH` tag internal objects.

---

## DataCite Metadata Schema (v4.5+)

Used by Zenodo, Figshare, Dryad, Dataverse for DOI registration. Not embedded in HDF5 but defines the external metadata registered with the DOI.

### Mandatory Properties

| Property | Description |
|----------|-------------|
| Identifier (DOI) | The DOI itself |
| Creator | Author(s) with name, affiliation, ORCID |
| Title | Dataset title |
| Publisher | Repository name |
| PublicationYear | Year published |
| ResourceType | "Dataset" |

### Recommended Properties

| Property | Description |
|----------|-------------|
| Subject | Keywords from controlled vocabulary |
| Contributor | Additional contributors |
| Date | Relevant dates (created, issued, etc.) |
| RelatedIdentifier | Links to papers, other datasets, software |
| Description | Abstract |
| GeoLocation | Spatial coverage |

### RelatedIdentifier Types

For linking HDF5 datasets to papers:

```xml
<relatedIdentifier relatedIdentifierType="DOI"
                   relationType="IsSupplementTo">
    10.1234/paper.doi
</relatedIdentifier>

<relatedIdentifier relatedIdentifierType="DOI"
                   relationType="Requires">
    10.11578/dc.20180330.1
</relatedIdentifier>
```

The HDF Group recommends `relationType="Requires"` with DOI `10.11578/dc.20180330.1` for citing HDF5 software.

---

## h5rdmtoolbox (FAIR + RDF for HDF5)

Python package for FAIR research data management with HDF5. Bridges HDF5 attributes and semantic web.

Repository: https://github.com/matthiasprobst/h5RDMtoolbox
Docs: https://h5rdmtoolbox.readthedocs.io/

### Core Concept: RDF Triples in HDF5

HDF5 metadata maps to RDF triples:
- **Subject**: The HDF5 group or dataset (identified by IRI)
- **Predicate**: The attribute name (mapped to IRI)
- **Object**: The attribute value (can be IRI or literal)

### Setting IRIs on Attributes

```python
import h5rdmtoolbox as h5tbx

with h5tbx.File('data.h5', 'w') as h5:
    # Set subject IRI for the file
    h5.rdf.subject = 'https://doi.org/10.5281/zenodo.123456'

    # Create dataset with semantic metadata
    ds = h5.create_dataset('temperature', data=[20.1, 21.3, 19.8])
    ds.attrs['units'] = 'degC'

    # Map attribute name to ontology IRI
    h5.rdf['units'].predicate = 'http://qudt.org/schema/qudt/unit'

    # Set creator with ORCID
    h5.attrs['creator'] = 'Jane Smith'
    h5.rdf['creator'].predicate = 'http://purl.org/dc/terms/creator'
    h5.rdf['creator'].object = 'https://orcid.org/0000-0001-2345-6789'
```

### JSON-LD Export

```python
# Export HDF5 metadata as JSON-LD (for Zenodo sidecar)
import h5rdmtoolbox as h5tbx

with h5tbx.File('data.h5', 'r') as h5:
    jsonld = h5.rdf.to_jsonld()
    # Write alongside HDF5 file
    with open('data.jsonld', 'w') as f:
        json.dump(jsonld, f, indent=2)
```

### Zenodo Integration

```python
import h5rdmtoolbox as h5tbx

# Upload to Zenodo with auto-generated JSON-LD
h5tbx.repository.zenodo.upload(
    'data.h5',
    auto_map_hdf=True,  # Generates JSON-LD sidecar from HDF5 attributes
    metadata={...}
)
```

When `auto_map_hdf=True`, the toolbox scans for `.hdf`, `.hdf5`, `.h5` suffixes and generates a JSON-LD metadata file, improving discoverability for binary HDF5 files.

---

## Validation Tools

### CF Checker

```bash
pip install cfchecker
cfchecks data.h5
# or
cfchecks -v auto data.nc
```

Validates CF Convention compliance: standard names, units, coordinate variables, etc.

### IOOS Compliance Checker

```bash
pip install compliance-checker
compliance-checker --test=acdd data.h5
compliance-checker --test=cf:1.8 data.h5
```

Checks ACDD 1.3 and CF compliance. Reports required, recommended, and suggested attributes.

### h5rdmtoolbox Validation

```python
import h5rdmtoolbox as h5tbx

with h5tbx.File('data.h5', 'r') as h5:
    # Check for FAIR attributes
    report = h5.rdf.validate()
    print(report)
```

---

## FAIR Principles Mapped to HDF5

| FAIR Principle | HDF5 Implementation |
|---------------|---------------------|
| **F1**: Globally unique persistent ID | `id` attribute with DOI; deposit in repository that mints DOIs |
| **F2**: Rich metadata | CF + ACDD attributes on root group and all datasets |
| **F3**: Metadata includes data ID | `id` attribute in file matches deposited DOI |
| **F4**: Registered in searchable resource | Deposit in Zenodo/Dataverse/etc. with keywords |
| **A1**: Retrievable by ID via standard protocol | DOI resolves to HTTPS landing page with download link |
| **A1.1**: Open protocol | HTTPS |
| **A1.2**: Auth where necessary | Repository handles auth (Earthdata Login, etc.) |
| **A2**: Metadata accessible even if data isn't | Repository preserves metadata record even if embargo/deletion |
| **I1**: Formal knowledge representation | `Conventions` attribute declares CF/ACDD; h5rdmtoolbox adds RDF |
| **I2**: FAIR vocabularies | CF Standard Name Table, GCMD keywords |
| **I3**: Qualified references | `relatedIdentifiers` in DataCite; `references` attribute in file |
| **R1**: Rich description | `summary`, `history`, `source`, `processing_level` |
| **R1.1**: Clear license | `license` attribute (SPDX identifier) |
| **R1.2**: Provenance | `history`, `source`, `creator_name`, `institution` |
| **R1.3**: Community standards | CF Conventions, ACDD, HDF-EOS5 |
