---
name: hdf5-vol-usage
description: "using VOL connector", "VOL usage", "how to use DAOS/Async/Cache/REST VOL", "switching to VOL connector", "HDF5_VOL_CONNECTOR environment variable", registering/configuring VOL connectors in applications.
version: 0.1.0
---

# Using HDF5 VOL Connectors

## Purpose

Guide for using existing HDF5 Virtual Object Layer (VOL) connectors to access alternative storage backends (object stores, async I/O, caching layers) while maintaining standard HDF5 API.

**Related skills**: `hdf5-vol-dev` for developing connectors, `hdf5-parallel` for MPI I/O.

## What VOL Connectors Do

Intercept storage-oriented HDF5 API calls and redirect to alternative backends. Get familiar HDF5 API but map storage to systems that better meet needs - object stores, databases, web services, custom formats.

**Key benefit**: Often requires zero or minimal code changes.

## Available Connectors

| Name | Value | Purpose |
|------|-------|---------|
| native | 0 | Standard \*.h5 files (built-in) |
| async | 512 | Non-blocking I/O |
| cache | 513 | Multi-tier caching |
| log | 514 | Sequential write optimization |
| daos | 4004 | Distributed object storage (DAOS) |
| rest | 520 | Web-based REST API access |
| lowfive | 521 | In situ workflows (memory+file) |
| pdc | 519 | Parallel Data Caching system |

Full list: https://support.hdfgroup.org/documentation/hdf5-docs/registered_vol_connectors.html

## Method 1: Environment Variables (No Code Changes)

```bash
export HDF5_PLUGIN_PATH=/path/to/connector/lib
export HDF5_VOL_CONNECTOR="connector_name"
./my_hdf5_app input.h5
```

**Example - DAOS VOL:**
```bash
export HDF5_PLUGIN_PATH=/opt/daos-vol/lib
export HDF5_VOL_CONNECTOR="daos"
mpirun -np 16 ./simulation
```

**Plugin paths:**
- Default (Linux): `/usr/local/hdf5/lib/plugin`
- Default (Windows): `%ALLUSERSPROFILE%/hdf5/lib/plugin`
- Override with `HDF5_PLUGIN_PATH`

**Limitations**: Works if app doesn't use native-only APIs. Check connector docs for API support.

## Method 2: Programmatic Registration

**Python (h5py):**
```python
import h5py

vol_id = h5py.h5vol.register_connector_by_name("async")
fapl = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
h5py.h5p.set_vol(fapl, vol_id, None)

fid = h5py.h5f.create("data.h5", flags=h5py.h5f.ACC_TRUNC, fapl=fapl)
f = h5py.File(fid)

dset = f.create_dataset("data", (1000, 1000), dtype='f')
dset[:] = 42.0
f.close()
```

**C API:**
```c
#include "hdf5.h"

hid_t vol_id = H5VLregister_connector_by_name("daos", H5P_DEFAULT);
hid_t fapl = H5Pcreate(H5P_FILE_ACCESS);
H5Pset_vol(fapl, vol_id, NULL);

hid_t file = H5Fcreate("data.h5", H5F_ACC_TRUNC, H5P_DEFAULT, fapl);
// Use HDF5 API normally...

H5Fclose(file);
H5Pclose(fapl);
H5VLclose(vol_id);
```

**By value** (when you know numeric ID):
```c
hid_t vol_id = H5VLregister_connector_by_value(512, H5P_DEFAULT);  // Async
```

## Method 3: Connector-Specific APIs

Some connectors provide convenience functions:

```c
#include "daos_vol_public.h"
hid_t fapl = H5Pcreate(H5P_FILE_ACCESS);
H5Pset_fapl_daos(fapl, "pool_uuid", "container_uuid");
// Use fapl in H5Fcreate/H5Fopen
```

## Querying Connectors

**Check registration:**
```c
htri_t is_reg = H5VLis_connector_registered_by_name("async");
```

**Get info from object:**
```c
hid_t vol_id = H5VLget_connector_id(file_id);
ssize_t name_size = H5VLget_connector_name(file_id, NULL, 0);
char *name = malloc(name_size + 1);
H5VLget_connector_name(file_id, name, name_size + 1);
printf("Connector: %s\n", name);
free(name);
H5VLclose(vol_id);
```

**Check if native:**
```c
htri_t is_native;
H5VLobject_is_native(file_id, &is_native);
```

**Query capabilities:**
```c
uint64_t flags;
H5VLquery_optional(vol_id, H5VL_SUBCLS_DATASET,
                    H5VL_NATIVE_DATASET_DIRECT_CHUNK_READ, &flags);
```

## Connector Stacking

Layer pass-through over terminal connectors:

```bash
# Cache over Async over Native
export HDF5_VOL_CONNECTOR="cache under_vol=512;under_info={under_vol=0;}"
```

**Common stacks:**
- `Cache → Native`: Fast tier caching
- `Async → Native`: Non-blocking I/O to disk
- `Cache → Async → Native`: Cached async writes

## Requirements

**HDF5 Version**: Most connectors require 1.14.0+ for proper support.

**API Compatibility**: Not all connectors support full HDF5 API. Variable-length datatypes, format-specific ops often unsupported.

**Token-based APIs**: For non-native connectors:
- ✅ `H5Fis_accessible()` not `H5Fis_hdf5()`
- ✅ `H5Oget_info3()` not `H5Oget_info1/2()`
- ✅ `H5Oopen_by_token()` not `H5Oopen_by_addr()`

## Connector-Specific Guidance

**Async VOL (512):**
- Best for write-heavy with computation overlap
- Set: `HDF5_ASYNC_EXE_FCLOSE=1`
- Avoid mixing sync/async (deadlock risk)
- Position `H5*exists`, `H5*get*` early (always sync)

**Cache VOL (513):**
- Multi-tier (SSD/HDD, memory/SSD)
- Stack over Async: `cache under_vol=512`
- Best for repeated reads

**DAOS VOL (4004):**
- Distributed object storage for exascale
- Auto-chunking: `HDF5_DAOS_CHUNK_TARGET_SIZE=8388608` (8 MiB)
- Requires DAOS pool/container UUIDs
- Better parallel scalability than POSIX

**Log-based VOL (514):**
- Optimizes parallel writes by sequential arrangement
- Reduces lock contention in MPI
- Best for write-intensive parallel

**REST VOL (520):**
- Access via HTTP
- Configure server URL
- Debug: `HDF5_REST_VOL_DEBUG=1`

## Best Practices

- ✅ Test with representative workloads
- ✅ Check API compatibility
- ✅ Use environment variables for testing
- ✅ Query capabilities for optional ops
- ✅ Profile performance gains
- ✅ Read connector docs for tuning

**Async VOL:**
- ✅ Separate I/O from compute/communication
- ✅ Cache HDF5 IDs vs repeated `H5*get*`
- ❌ Don't mix sync/async
- ❌ Don't interleave I/O with sync queries

**General:**
- ✅ Use token-based APIs
- ✅ Handle loading failures
- ✅ Document requirements
- ✅ Test native first, then switch

## Common Issues

**Connector fails to load:**
- Check `HDF5_PLUGIN_PATH`
- Verify library exists with permissions
- Ensure HDF5 1.14.0+ compatibility
- Check connector name

**Unsupported operation:**
- Connector has incomplete API
- Use `H5VLquery_optional()`
- Fall back to native

**Async VOL no improvement:**
- Insufficient compute overlap
- Sync APIs blocking
- Adjust triggers: `HDF5_ASYNC_MAX_DELAY_TIME=1000`

**Worse performance:**
- Connector overhead
- Missing optimizations
- Configuration not tuned
- Consider alternatives

**Mixing errors:**
- Don't open same file with different connectors
- Use consistent FAPL
- Close before switching

## Debugging

```bash
# Check environment
echo $HDF5_VOL_CONNECTOR
echo $HDF5_PLUGIN_PATH

# Enable debugging
export HDF5_ASYNC_API_DEBUG_LEVEL=1  # Async
export HDF5_REST_VOL_DEBUG=1         # REST
export HDF5_DEBUG=all                # General
```

**Check loaded connector:**
```c
hid_t vol_id = H5VLget_connector_id(file_id);
char name[256];
H5VLget_connector_name(file_id, name, sizeof(name));
printf("Active: %s\n", name);
```

## Command-Line Tools

```bash
h5dump --vol-name=async data.h5
h5ls --vol-value=512 data.h5
h5diff --vol-name=daos file1.h5 file2.h5
h5repack --vol-name=cache input.h5 output.h5
```

Supports: h5dump, h5ls, h5diff, h5mkgrp, h5repack

## When to Use

**Use VOL when:**
- Need alternative storage backend
- Want async I/O without refactoring
- Need multi-tier caching
- Accessing via web services
- Optimizing parallel writes

**Stick with native when:**
- Standard file storage sufficient
- Need full API compatibility
- Performance critical with overhead concerns
- Simple use case

## Citations and References

- [VOL User Guide](https://support.hdfgroup.org/documentation/hdf5/latest/_h5_v_l__u_g.html)
- [VOL API Reference](https://support.hdfgroup.org/documentation/hdf5/latest/group___h5_v_l.html)
- [Registered Connectors](https://support.hdfgroup.org/documentation/hdf5-docs/registered_vol_connectors.html)
- [DAOS VOL](https://github.com/HDFGroup/vol-daos)
- [Async VOL Best Practices](https://hdf5-vol-async.readthedocs.io/en/latest/bestpractice.html)
- [HDF Forum VOL Env Vars](https://forum.hdfgroup.org/t/vol-environment-variables/10416)
