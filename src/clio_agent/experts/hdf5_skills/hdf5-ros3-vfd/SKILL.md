---
name: hdf5-ros3-vfd
description: "ros3 VFD", "read-only S3", "HDF5 from S3", "H5Pset_fapl_ros3", "S3 byte-range", "remote HDF5 access", "HDF5 S3 driver", reading HDF5 from Amazon S3 or S3-compatible storage, configuring ros3 driver.
version: 0.1.0
---

# HDF5 ROS3 VFD (Read-Only S3 Driver)

## Purpose

The ROS3 (Read-Only S3) Virtual File Driver enables transparent, read-only access to HDF5 files stored in Amazon S3 or S3-compatible object storage. It translates HDF5 read operations into HTTP byte-range GET requests at the VFD layer, allowing selective access to remote datasets and metadata without downloading entire files.

**Related skills**: `hdf5-cloud-optimized` for writing files optimized for S3 access, `hdf5-chunking` for chunk size configuration.

## What Problem ROS3 VFD Solves

Accessing HDF5 files in cloud object storage traditionally requires downloading the entire file first, which is impractical for large scientific datasets. ROS3 VFD eliminates this -- the application reads datasets, attributes, and metadata using normal HDF5 API calls while the driver handles all HTTP communication transparently.

## Installation

**pip-installed h5py does NOT include ros3 support.** Use conda-forge:

```bash
conda install -c conda-forge h5py
```

Or build h5py from source against an HDF5 library built with ROS3 enabled:
- **HDF5 < 2.0**: CMake: `HDF5_ENABLE_ROS3_VFD=ON`. Autotools: `--enable-ros3-vfd`. Requires libcurl and OpenSSL.
- **HDF5 2.0+**: Requires the aws-c-s3 library (replaces libcurl/OpenSSL).

Verify ros3 is available:
```python
import h5py
print("ros3" in h5py.registered_drivers())
```

## h5py API Usage

### Anonymous Access (Public Buckets)

```python
import h5py

# HTTPS URL format
f = h5py.File(
    "https://s3.amazonaws.com/bucket/file.h5",
    mode="r",
    driver="ros3"
)

# S3 URI format (h5py >= 3.8 or HDF5 >= 2.0)
f = h5py.File("s3://bucket/file.h5", mode="r", driver="ros3")

data = f["dataset"][:]
f.close()
```

### Authenticated Access

```python
# All credential parameters must be bytes, not str
f = h5py.File(
    "s3://bucket/file.h5",
    mode="r",
    driver="ros3",
    aws_region=b"us-east-1",
    secret_id=b"AKIAIOSFODNN7EXAMPLE",
    secret_key=b"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
)
```

### With Session Token (Temporary Credentials)

```python
# Requires h5py >= 3.10, HDF5 >= 1.14.2
# Needed for AWS STS, NASA Earthdata, and similar
f = h5py.File(
    "s3://bucket/file.h5",
    mode="r",
    driver="ros3",
    aws_region=b"us-east-1",
    secret_id=b"AKIAIOSFODNN7EXAMPLE",
    secret_key=b"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    session_token=b"FwoGZXIvYXdz...",
)
```

### Automatic Credentials (HDF5 2.0+)

```python
# HDF5 2.0+ resolves credentials automatically from standard AWS sources
# No need to pass explicit credentials
f = h5py.File("s3://bucket/file.h5", mode="r", driver="ros3")
# Driver checks: env vars -> ~/.aws/credentials -> ~/.aws/config -> IAM role
```

### h5py Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `aws_region` | `bytes` | `b""` | AWS region of the S3 bucket |
| `secret_id` | `bytes` | `b""` | AWS access key ID |
| `secret_key` | `bytes` | `b""` | AWS secret access key |
| `session_token` | `bytes` | `b""` | AWS temporary session token (h5py >= 3.10) |

All three of `aws_region`, `secret_id`, and `secret_key` must be provided together to activate explicit authentication (h5py >= 3.8).

**Supported URL formats**: `http://`, `https://`, `s3://` (s3:// requires h5py >= 3.8 or HDF5 >= 2.0).

## C API Usage

### Anonymous Access

```c
H5FD_ros3_fapl_t ros3_config;
ros3_config.version = H5FD_CURR_ROS3_FAPL_T_VERSION;
ros3_config.authenticate = 0;
strcpy(ros3_config.aws_region, "");
strcpy(ros3_config.secret_id, "");
strcpy(ros3_config.secret_key, "");

hid_t fapl = H5Pcreate(H5P_FILE_ACCESS);
H5Pset_fapl_ros3(fapl, &ros3_config);

hid_t file = H5Fopen("https://s3.amazonaws.com/bucket/file.h5",
                      H5F_ACC_RDONLY, fapl);
/* ... read data ... */
H5Fclose(file);
H5Pclose(fapl);
```

### Authenticated Access

```c
H5FD_ros3_fapl_t ros3_config;
ros3_config.version = H5FD_CURR_ROS3_FAPL_T_VERSION;
ros3_config.authenticate = 1;
strcpy(ros3_config.aws_region, "us-east-1");
strcpy(ros3_config.secret_id, "AKIAIOSFODNN7EXAMPLE");
strcpy(ros3_config.secret_key, "wJalrXUtnFEMI/...");

hid_t fapl = H5Pcreate(H5P_FILE_ACCESS);
H5Pset_fapl_ros3(fapl, &ros3_config);

/* Add session token for temporary credentials (HDF5 >= 1.14.2) */
H5Pset_fapl_ros3_token(fapl, "FwoGZXIvYXdz...");
```

The `H5FD_ros3_fapl_t` struct has fields: `version`, `authenticate` (bool), `aws_region[33]`, `secret_id[129]`, `secret_key[129]`. When `authenticate` is false on HDF5 2.0+, the driver searches the standard AWS credential chain automatically.

## Authentication Methods

1. **Anonymous**: No credentials provided. Works for public buckets.
2. **Explicit credentials**: Provide `aws_region`, `secret_id`, `secret_key` directly.
3. **Session tokens**: Add via `session_token` kwarg (h5py) or `H5Pset_fapl_ros3_token()` (C). Required for AWS STS, NASA Earthdata.
4. **Automatic resolution** (HDF5 2.0+): Standard AWS credential chain -- environment variables, `~/.aws/credentials`, `~/.aws/config`, EC2 instance metadata / IAM roles. Just open the file with `driver="ros3"` and no explicit credentials.

## S3-Compatible Object Storage

ROS3 works with MinIO, Ceph RADOS Gateway, and other S3-compatible storage:

**HDF5 2.0+**: Use the `AWS_ENDPOINT_URL_S3` env var or `--endpoint-url` flag for HDF5 CLI tools.

**Pre-2.0**: Use `http://` or `https://` URLs pointing directly at the endpoint:
```python
f = h5py.File(
    "http://minio-host:9000/bucket/file.h5",
    mode="r",
    driver="ros3",
    aws_region=b"us-east-1",
    secret_id=b"minioaccess",
    secret_key=b"miniosecret",
)
```

## HDF5 CLI Tools with ROS3

```bash
# List contents of an S3-hosted HDF5 file
h5ls --vfd=ros3 "s3://bucket/file.h5"

# Dump dataset from S3
h5dump --vfd=ros3 -d /dataset "s3://bucket/file.h5"

# With custom endpoint (HDF5 2.0+)
h5ls --vfd=ros3 --endpoint-url="http://minio:9000" "s3://bucket/file.h5"
```

## Environment Variables

| Variable | Purpose |
|---|---|
| `AWS_ACCESS_KEY_ID` | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_SESSION_TOKEN` | Session token for temporary credentials |
| `AWS_REGION` | AWS region (checked first) |
| `AWS_DEFAULT_REGION` | Fallback region |
| `AWS_PROFILE` | Select non-default AWS profile |
| `AWS_CONFIG_FILE` | Override path to `~/.aws/config` |
| `AWS_SHARED_CREDENTIALS_FILE` | Override path to `~/.aws/credentials` |
| `AWS_ENDPOINT_URL_S3` | Custom S3 endpoint URL (HDF5 2.0+) |
| `AWS_ENDPOINT_URL` | Fallback custom endpoint URL (HDF5 2.0+) |

## Common Patterns

**Read a subset from a large remote dataset**:
```python
with h5py.File("s3://bucket/large.h5", "r", driver="ros3") as f:
    # Only fetches the chunks covering this slice
    subset = f["temperature"][0:100, 500:600]
```

**Explore structure of a remote file**:
```python
with h5py.File("s3://bucket/data.h5", "r", driver="ros3") as f:
    f.visititems(lambda name, obj: print(name, type(obj).__name__))
```

**NASA Earthdata access**:
```python
import earthaccess

# earthaccess handles token management
auth = earthaccess.login()
s3_creds = auth.get_s3_credentials(daac="NSIDC")

f = h5py.File(
    "s3://nsidc-cumulus-prod/path/to/granule.h5",
    "r",
    driver="ros3",
    aws_region=b"us-west-2",
    secret_id=s3_creds["accessKeyId"].encode(),
    secret_key=s3_creds["secretAccessKey"].encode(),
    session_token=s3_creds["sessionToken"].encode(),
)
```

## HDF5 2.0 Improvements

HDF5 2.0.0 (November 2025) significantly rewrote the ROS3 VFD. Key changes for users:

- **Automatic credential resolution** -- no need to pass credentials explicitly in most cases
- **Parallel S3 requests** -- large reads automatically split into concurrent requests via aws-c-s3
- **Default 64 MiB page buffer** -- metadata and data pages cached automatically
- **Default 8 MiB chunk cache** -- set automatically when using ROS3
- **Native `s3://` URI support** -- no longer requires h5py URL translation
- **Custom endpoint support** -- `AWS_ENDPOINT_URL_S3` env var and `--endpoint-url` CLI flag
- **Smart retry** -- transient S3 failures retried automatically

Prior to 2.0, the driver used libcurl with serial (one-at-a-time) requests and no automatic caching.

## Performance Tips

- **Cloud-optimize files before uploading** -- use paged aggregation and 10-100 MB chunks (see `hdf5-cloud-optimized` skill)
- **Co-locate compute and storage** -- run in the same AWS region as the S3 bucket
- **Read chunk-aligned slices** -- avoid element-by-element or small random access
- **Enable page buffering on HDF5 < 2.0** -- call `H5Pset_page_buffer()` explicitly (2.0+ does this automatically)
- **Use `h5repack -S PAGE -G <pagesize>`** to retroactively cloud-optimize existing files
- **Profile S3 request patterns** -- use [ros3vfd-log-info](https://github.com/ajelenak/ros3vfd-log-info) to analyze and optimize

## Limitations

- **Read-only**: Cannot create or modify files on S3. No write counterpart exists.
- **Requires special build**: pip-installed h5py does not include ros3. Use conda-forge or build from source.
- **Network dependent**: Performance tied to S3 latency and bandwidth.
- **File layout matters**: Files without paged aggregation generate excessive metadata requests and perform poorly.
- **Serial I/O on < 2.0**: Each HDF5 library read becomes a separate HTTP request (fixed in 2.0+).

## When to Use ROS3 VFD

- Reading HDF5 files directly from S3 without downloading them
- Accessing large remote datasets where only a subset is needed
- Building pipelines that process cloud-hosted scientific data in place
- Accessing public HDF5 archives (NASA Earthdata, NOAA, etc.)
- Replacing manual download-process-delete workflows

## When NOT to Use ROS3 VFD

- Need to write or modify files on S3 (ros3 is read-only)
- Accessing entire large files repeatedly (download once instead)
- Low-bandwidth or high-latency network connections
- Files not optimized for cloud access (many small chunks, scattered metadata)
- Need concurrent write access (use HSDS or a different architecture)

## Gotchas

- **Credentials must be `bytes` in h5py**: `b"us-east-1"` not `"us-east-1"` -- passing `str` causes cryptic errors
- **"undefined symbol: H5Pset_fapl_ros3"**: h5py linked against HDF5 built without ROS3 support. Install via conda-forge or rebuild.
- **pip h5py lacks ros3**: The default pip wheel does not include ros3. Use `conda install -c conda-forge h5py`.
- **Pre-h5py 3.8 auth bug**: Setting any single credential kwarg triggered authentication, causing failures. Fixed in 3.8 (now requires all three).
- **Region is required**: If the driver cannot determine an AWS region from any source, file open fails silently or with an unhelpful error.
- **Don't disable page buffer on 2.0**: Explicitly disabling the default page buffer causes severe performance degradation.
- **Non-optimized files are slow**: Files without paged aggregation can require hundreds of small HTTP requests just to parse metadata.

## Version Requirements

| Component | Version | Feature |
|---|---|---|
| HDF5 | 1.10.6+ | ROS3 VFD introduced |
| HDF5 | 1.14.2+ | Session token support (`H5Pset_fapl_ros3_token`) |
| HDF5 | 1.14.3+ | 16 MiB initial file cache on open |
| HDF5 | 2.0.0+ | aws-c-s3 backend, auto credentials, parallel requests, `s3://` URIs, custom endpoints |
| h5py | 3.2+ | Initial ros3 driver support |
| h5py | 3.8+ | `s3://` URL support, fixed auth activation logic |
| h5py | 3.10+ | `session_token` parameter |

## Citations and References

- [HDF Group: Virtual File Drivers Documentation](https://support.hdfgroup.org/documentation/hdf5/latest/_h5_f_d__u_g.html)
- [HDF Group: File Access Properties (H5Pset_fapl_ros3)](https://support.hdfgroup.org/documentation/hdf5/latest/group___f_a_p_l.html)
- [HDF Group: Registered Virtual File Drivers](https://support.hdfgroup.org/documentation/hdf5-docs/registered_virtual_file_drivers_vfds.html)
- [HDF Group: Cloud Storage Options for HDF5](https://www.hdfgroup.org/2022/08/08/cloud-storage-options-for-hdf5/)
- [HDF Group: HDF5 2.0.0 High-Performance S3 Access](https://forum.hdfgroup.org/t/hdf5-2-0-0-high-performance-hdf5-data-access-directly-from-s3/13529)
- [h5py: File Objects (ros3 driver documentation)](https://docs.h5py.org/en/stable/high/file.html)
- [Cloud-Optimized HDF/NetCDF Guide](https://guide.cloudnativegeo.org/cloud-optimized-netcdf4-hdf5/)
- [GitHub: ros3vfd-log-info (S3 request analysis tool)](https://github.com/ajelenak/ros3vfd-log-info)
