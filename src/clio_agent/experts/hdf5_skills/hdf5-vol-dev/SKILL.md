---
name: hdf5-vol-dev
description: "developing VOL connector", "creating VOL connector", "VOL connector development", "H5VL_class_t", "VOL callbacks", "terminal vs pass-through connector", implementing VOL connector callbacks, developing custom HDF5 VOL connectors.
version: 0.1.0
---

# Developing HDF5 VOL Connectors

## Purpose

Guide for developing custom HDF5 Virtual Object Layer (VOL) connectors to integrate alternative storage backends or add functionality (caching, logging, encryption, async I/O).

**Related skills**: `hdf5-vol-usage` for using existing connectors.

## Overview

VOL connectors intercept storage-oriented HDF5 API calls. When applications call `H5Fcreate()`, `H5Dwrite()`, etc., the library invokes callbacks in your connector.

**Architecture**: VOL sits between public API and storage. Only storage operations pass through VOL - dataspace, property lists, error stacks bypass it.

## Connector Types

**Terminal**: Interface directly with storage (don't forward). Examples: DAOS VOL, REST VOL. Use for complete storage replacement.

**Pass-through**: Augment then forward to underlying connector. Can be stacked. Examples: Async VOL, Cache VOL. Use for adding functionality.

**Stacking**: `Cache VOL → Async VOL → Native VOL`

## Prerequisites

- HDF5 1.14.0+ (CRITICAL - 1.12 binary incompatible)
- CMake 2.8.12.2+, C compiler
- Target storage backend SDK
- HDF5 API familiarity, C function pointers

## Getting Started

```bash
git clone --recursive https://github.com/HDFGroup/vol-toolkit
cd vol-toolkit
```

**Templates:**
- `vol-template`: Terminal connector shell
- `vol-external-passthrough`: Pass-through shell
- `vol-tests`: Test suite

## H5VL_class_t Structure

```c
typedef struct H5VL_class_t {
    unsigned version;                    // Set to H5VL_VERSION (CRITICAL)
    H5VL_class_value_t value;           // Connector ID (256-511 for testing)
    const char *name;                    // MUST be unique!
    unsigned conn_version;               // Your version
    uint64_t cap_flags;                 // Capability flags

    herr_t (*initialize)(hid_t vipl_id);
    herr_t (*terminate)(void);

    const H5VL_file_class_t      *file_cls;
    const H5VL_dataset_class_t   *dataset_cls;
    const H5VL_group_class_t     *group_cls;
    const H5VL_attr_class_t      *attr_cls;
    const H5VL_link_class_t      *link_cls;
    const H5VL_object_class_t    *object_cls;
    const H5VL_datatype_class_t  *datatype_cls;
    const H5VL_request_class_t   *request_cls;    // Async
    const H5VL_blob_class_t      *blob_cls;
    const H5VL_token_class_t     *token_cls;      // Object tokens
    const H5VL_wrap_class_t      *wrap_cls;
    const H5VL_introspect_class_t *introspect_cls;

    herr_t (*optional)(void *obj, ...);
} H5VL_class_t;
```

## Callback Classes

Each `*_cls` contains function pointers for operations:

**File** (`H5VL_file_class_t`): `create`, `open`, `get`, `specific`, `optional`, `close`

**Dataset** (`H5VL_dataset_class_t`): `create`, `open`, `read`, `write`, `get`, `specific`, `optional`, `close`

**Group/Attribute/Link/Object/Datatype**: Similar patterns

**Mapping**: H5Fcreate → file_cls->create, H5Dwrite → dataset_cls->write, etc.

## Minimal Terminal Example

```c
#include "H5VLconnector.h"

typedef struct { char *filename; void *storage_handle; } my_file_t;

static void *my_file_create(const char *name, unsigned flags,
                             hid_t fcpl_id, hid_t fapl_id,
                             hid_t dxpl_id, void **req) {
    my_file_t *file = malloc(sizeof(my_file_t));
    file->filename = strdup(name);
    file->storage_handle = my_storage_create(name);
    return (void *)file;
}

static herr_t my_file_close(void *obj, hid_t dxpl_id, void **req) {
    my_file_t *file = (my_file_t *)obj;
    my_storage_close(file->storage_handle);
    free(file->filename);
    free(file);
    return 0;
}

static H5VL_file_class_t my_file_cls = {
    .create = my_file_create, .close = my_file_close,
    /* NULL for unimplemented */
};

static H5VL_class_t my_connector_cls = {
    .version = H5VL_VERSION, .value = 256, .name = "my_connector",
    .conn_version = 1, .file_cls = &my_file_cls,
    /* other classes NULL */
};

H5PL_type_t H5PLget_plugin_type(void) { return H5PL_TYPE_VOL; }
const void *H5PLget_plugin_info(void) { return &my_connector_cls; }
```

## Pass-Through Pattern

```c
static void *my_pt_file_create(const char *name, unsigned flags,
                                 hid_t fcpl_id, hid_t fapl_id,
                                 hid_t dxpl_id, void **req) {
    // Augment
    printf("Creating: %s\n", name);

    // Forward to underlying connector
    hid_t under_vol_id = get_underlying_vol_id(fapl_id);
    void *under_file = H5VLfile_create(name, flags, fcpl_id, fapl_id,
                                        dxpl_id, req, under_vol_id);

    // Wrap and return
    my_wrap_t *wrap = malloc(sizeof(my_wrap_t));
    wrap->under_object = under_file;
    wrap->under_vol_id = under_vol_id;
    return wrap;
}
```

## Connector Registration

**ID ranges:**
- 0-255: Library (requires HDF Group approval)
- 256-511: Testing (free during development)
- 512+: Production (contact HDF Help Desk)

**As dynamic plugin:**
```c
// Build as shared library
gcc -shared -fPIC -o libh5my.so my_connector.c -lhdf5

// Implement H5PLget_plugin_type() and H5PLget_plugin_info() (see example above)
```

## Token-Based References

For non-file backends, implement `token_cls` to map `H5O_token_t` to backend-specific IDs (UUIDs, database keys, URLs):

```c
typedef struct H5VL_token_class_t {
    herr_t (*cmp)(void *obj, const H5O_token_t *token1,
                  const H5O_token_t *token2, int *cmp_value);
    herr_t (*to_str)(void *obj, H5I_type_t obj_type,
                     const H5O_token_t *token, char **token_str);
    herr_t (*from_str)(void *obj, H5I_type_t obj_type,
                       const char *token_str, H5O_token_t *token);
} H5VL_token_class_t;
```

## Testing

```bash
cd vol-tests/build
export HDF5_VOL_CONNECTOR="my_connector"
export HDF5_PLUGIN_PATH=/path/to/libh5my.so
ctest . -VV
```

**Test incrementally**: File ops → Groups → Datasets → Attributes → Links → Advanced

## Best Practices

**Development:**
- ✅ Use VOL toolkit templates
- ✅ Target HDF5 1.14.0+ only
- ✅ Set `version = H5VL_VERSION`
- ✅ Make name globally unique
- ✅ NULL unimplemented callbacks
- ✅ Use testing range (256-511)
- ✅ Implement incrementally
- ✅ Document unsupported operations

**Pass-through:**
- ✅ Always forward operations
- ✅ Maintain under_vol_id reference
- ✅ Wrap/unwrap objects correctly
- ✅ Forward async requests

**Memory:**
- ✅ Allocate context for objects
- ✅ Free in close callbacks
- ✅ Track reference counts
- ✅ No leaks on errors

**Error handling:**
- ✅ Return negative on error
- ✅ Set HDF5 error stack
- ✅ Clean up partial operations
- ✅ Validate inputs

## Common Pitfalls

- ❌ Using HDF5 1.12 (binary incompatible) → Use 1.14.0+
- ❌ Wrong `version` field → Set to H5VL_VERSION
- ❌ NULL callbacks for used operations → Implement incrementally
- ❌ Memory leaks in close callbacks → Use Valgrind
- ❌ Forgetting to forward (pass-through) → Follow template
- ❌ Not implementing token_cls → Required for non-file backends
- ❌ Name collisions → Choose unique name

## Advanced Features

**Async operations** (request_cls): Non-blocking I/O, return request handle, complete in background

**Connector-specific operations** (optional callback): Extend HDF5 API with custom ops

**Introspection** (introspect_cls): Report capabilities, support H5VLquery_optional()

## Debugging

```c
// Verbose output
fprintf(stderr, "MyVOL: file_create(%s)\n", name);

// Check registration
htri_t reg = H5VLis_connector_registered_by_value(256);

// Validate invocations
assert(name != NULL);

// GDB
gdb --args ./test_app
(gdb) break my_file_create
```

## Performance Notes

- Callback overhead per layer
- External connectors lack native HDF5 caches (implement your own)
- Batch operations to reduce backend round-trips
- Ensure thread-safety if multi-threaded

## Example Connectors

**Simple**: Pass-through VOL (reference)

**Moderate**: Cache VOL, Async VOL

**Advanced**: DAOS VOL, REST VOL, PDC VOL

All at: https://github.com/HDFGroup

## Registering Production Connector

Contact help@hdfgroup.org with: name, value request, contact, description. Get value > 511, listed officially.

## Build System

```cmake
cmake_minimum_required(VERSION 3.10)
project(MyVOL C)
find_package(HDF5 1.14.0 REQUIRED)
add_library(h5my SHARED my_connector.c)
target_link_libraries(h5my HDF5::HDF5)
install(TARGETS h5my DESTINATION lib/hdf5/plugin)
```

## When to Develop

**Good reasons**: Custom storage backend, add functionality, optimize patterns, research, proprietary integration

**Consider first**: Existing connector? VFD simpler? Native adequate?

## Citations and References

- [VOL User Guide](https://support.hdfgroup.org/documentation/hdf5/latest/_h5_v_l__u_g.html)
- [VOL Toolkit](https://github.com/HDFGroup/vol-toolkit)
- [VOL Tests](https://github.com/HDFGroup/vol-tests)
- [H5VL_class_t Reference](https://portal.hdfgroup.org/documentation/hdf5/latest/struct_h5_v_l__class__t.html)
- [HDF Forum (VOLs)](https://forum.hdfgroup.org/)
- [VOL Tutorial (2022)](https://www.hdfgroup.org/wp-content/uploads/2022/02/VOL_tutorial_feb_2022.pdf)
