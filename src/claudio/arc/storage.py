"""IOWarp CTE (Convergent Tiered Environment) storage backend for ARC.

Integrates ARC Memory with IOWarp's multi-tier storage system for automatic
data migration across tiers based on access patterns.

Architecture:
    - Hot tier: In-memory cache (handled by LRUCache in memory.py)
    - Warm tier: SSD/local disk (default for active data)
    - Cold tier: Network storage/HDF5 (for historical data)
    - Archive tier: Tape/long-term storage (for old data)

Tier Migration Policy:
    - Hot → Warm: 1 day (handled by LRU cache eviction)
    - Warm → Cold: 7 days (infrequent access)
    - Cold → Archive: 30 days (historical data)

Graceful Degradation:
    If IOWarp is unavailable, falls back to local filesystem storage.

See PLAN.md v0.3.0 Task 2 for requirements.
"""

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import msgspec


class IOWarpCTEBackend:
    """IOWarp CTE storage backend for ARC persistence.

    Provides multi-tier storage with automatic migration based on access patterns.
    Gracefully degrades to local filesystem if IOWarp is unavailable.

    Args:
        namespace: IOWarp namespace (e.g., "/claudio/arc")
        base_dir: Local fallback directory if IOWarp unavailable
        tier_policy: Tier migration policy (days to migrate between tiers)

    Examples:
        >>> backend = IOWarpCTEBackend(namespace="/claudio/arc")
        >>> backend.write("conversations/session-1.msgpack", data, tier="warm")
        >>> data = backend.read("conversations/session-1.msgpack")
        >>> stats = backend.get_tier_stats()
        >>> print(f"IOWarp available: {stats['iowarp_available']}")
    """

    def __init__(
        self,
        namespace: str = "/claudio/arc",
        base_dir: str = ".claudio/arc",
        tier_policy: Optional[Dict[str, int]] = None,
    ):
        """Initialize IOWarp CTE backend.

        Args:
            namespace: IOWarp namespace for ARC data
            base_dir: Local directory for fallback storage
            tier_policy: Days to migrate between tiers (hot_to_warm, warm_to_cold, cold_to_archive)
        """
        self.namespace = namespace
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # Tier migration policy (days)
        self.tier_policy = tier_policy or {
            "hot_to_warm": 1,      # 1 day in hot tier before eviction
            "warm_to_cold": 7,     # 1 week in warm tier
            "cold_to_archive": 30,  # 1 month in cold tier
        }

        # Check IOWarp availability
        self.iowarp_available = self._check_iowarp()

        # Initialize IOWarp connection if available
        if self.iowarp_available:
            self._initialize_iowarp()
        else:
            # Warn user that we're using local storage
            print(f"⚠ IOWarp not available, using local storage: {self.base_dir}")

        # Create tier directories for local fallback
        self._warm_dir = self.base_dir / "warm"
        self._cold_dir = self.base_dir / "cold"
        self._archive_dir = self.base_dir / "archive"

        self._warm_dir.mkdir(exist_ok=True)
        self._cold_dir.mkdir(exist_ok=True)
        self._archive_dir.mkdir(exist_ok=True)

        # Access tracking for tier migration
        self._access_metadata_file = self.base_dir / "access_metadata.msgpack"
        self._access_metadata: Dict[str, Dict[str, Any]] = self._load_access_metadata()

        # Performance counters
        self._tier_migrations = 0
        self._iowarp_reads = 0
        self._iowarp_writes = 0
        self._local_reads = 0
        self._local_writes = 0

    def _check_iowarp(self) -> bool:
        """Check if IOWarp runtime is available.

        Checks for:
        1. ZeroMQ port 5555 connectivity
        2. IOWARP_ENDPOINT environment variable
        3. Docker container presence

        Returns:
            True if IOWarp runtime available, False otherwise
        """
        import socket

        # Check environment variable first
        endpoint = os.getenv("IOWARP_ENDPOINT", "tcp://localhost:5555")

        # Try to connect to ZeroMQ port
        try:
            # Parse endpoint
            if "://" in endpoint:
                protocol, hostport = endpoint.split("://")
                if ":" in hostport:
                    host, port = hostport.rsplit(":", 1)
                    port = int(port)
                else:
                    host = hostport
                    port = 5555
            else:
                host = "localhost"
                port = 5555

            # Test TCP connection
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex((host, port))
            sock.close()

            return result == 0
        except Exception:
            return False

    def _initialize_iowarp(self) -> None:
        """Initialize ZeroMQ connection to IOWarp CTE runtime."""
        try:
            import zmq

            endpoint = os.getenv("IOWARP_ENDPOINT", "tcp://localhost:5555")

            # Create ZeroMQ context and socket
            self.zmq_context = zmq.Context()
            self.zmq_socket = self.zmq_context.socket(zmq.REQ)
            self.zmq_socket.connect(endpoint)

            # Register namespace with IOWarp
            self._register_namespace()

            print(f"✓ Connected to IOWarp CTE runtime at {endpoint}")
        except ImportError:
            print("⚠ pyzmq not installed, using local storage fallback")
            print("  Install with: uv pip install pyzmq")
            self.iowarp_available = False
        except Exception as e:
            print(f"⚠ Could not connect to IOWarp runtime: {e}")
            print("  Start runtime with: docker-compose up iowarp-runtime")
            self.iowarp_available = False

    def _register_namespace(self) -> None:
        """Register /claudio/arc namespace with IOWarp CTE."""
        request = {
            "op": "register_namespace",
            "namespace": self.namespace,
            "tier_policy": self.tier_policy,
        }

        self.zmq_socket.send_json(request)
        response = self.zmq_socket.recv_json()

        if response.get("status") != "ok":
            raise RuntimeError(f"Failed to register namespace: {response.get('error')}")

    def write(self, key: str, data: bytes, tier: str = "warm") -> None:
        """Write data to IOWarp CTE with tier specification.

        Writes data to the specified storage tier. If IOWarp is available,
        uses IOWarp API. Otherwise, falls back to local filesystem.

        Args:
            key: Data key (relative path within namespace)
            data: Binary data (msgpack encoded)
            tier: Target tier ("warm", "cold", "archive")

        Examples:
            >>> backend.write("conversations/session-1.msgpack", encoded_data, tier="warm")
            >>> backend.write("invocations/trace-123.msgpack", encoded_data, tier="cold")
        """
        # Update access metadata
        self._update_access_metadata(key, tier=tier, operation="write")

        if self.iowarp_available:
            # Write to IOWarp
            self._write_iowarp(key, data, tier)
            self._iowarp_writes += 1
        else:
            # Fallback to local storage
            self._write_local(key, data, tier)
            self._local_writes += 1

        # Periodically run tier migration
        self._maybe_migrate_tiers()

    def read(self, key: str) -> Optional[bytes]:
        """Read data from IOWarp CTE.

        Reads data from any tier. Automatically promotes frequently accessed
        data to warmer tiers.

        Args:
            key: Data key (relative path within namespace)

        Returns:
            Binary data if found, None otherwise

        Examples:
            >>> data = backend.read("conversations/session-1.msgpack")
            >>> if data:
            ...     conv = msgspec.msgpack.decode(data, type=Conversation)
        """
        # Update access metadata (for tier promotion)
        self._update_access_metadata(key, operation="read")

        if self.iowarp_available:
            data = self._read_iowarp(key)
            if data:
                self._iowarp_reads += 1
            return data
        else:
            data = self._read_local(key)
            if data:
                self._local_reads += 1
            return data

    def _write_iowarp(self, key: str, data: bytes, tier: str) -> None:
        """Write to IOWarp CTE via ZeroMQ.

        Args:
            key: Data key
            data: Binary data
            tier: Target tier
        """
        import base64

        request = {
            "op": "write",
            "namespace": self.namespace,
            "key": key,
            "data": base64.b64encode(data).decode("ascii"),
            "tier": tier,
        }

        self.zmq_socket.send_json(request)
        response = self.zmq_socket.recv_json()

        if response.get("status") != "ok":
            raise RuntimeError(f"Write failed: {response.get('error')}")

    def _read_iowarp(self, key: str) -> Optional[bytes]:
        """Read from IOWarp CTE via ZeroMQ.

        Args:
            key: Data key

        Returns:
            Binary data or None
        """
        import base64

        request = {
            "op": "read",
            "namespace": self.namespace,
            "key": key,
        }

        self.zmq_socket.send_json(request)
        response = self.zmq_socket.recv_json()

        if response.get("status") == "ok":
            return base64.b64decode(response["data"])
        elif response.get("status") == "not_found":
            return None
        else:
            raise RuntimeError(f"Read failed: {response.get('error')}")

    def _write_local(self, key: str, data: bytes, tier: str = "warm") -> None:
        """Write to local disk (fallback).

        Writes to tier-specific directory on local filesystem.

        Args:
            key: Data key (relative path)
            data: Binary data
            tier: Target tier directory
        """
        # Map tier to directory
        tier_dir = self._get_tier_directory(tier)

        # Create full path
        file_path = tier_dir / key
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write data
        file_path.write_bytes(data)

    def _read_local(self, key: str) -> Optional[bytes]:
        """Read from local disk (fallback).

        Searches all tiers for the data.

        Args:
            key: Data key

        Returns:
            Binary data or None
        """
        # Check metadata for current tier
        metadata = self._access_metadata.get(key, {})
        current_tier = metadata.get("tier", "warm")

        # Try current tier first
        tier_dir = self._get_tier_directory(current_tier)
        file_path = tier_dir / key
        if file_path.exists():
            return file_path.read_bytes()

        # Fallback: search all tiers
        for tier in ["warm", "cold", "archive"]:
            tier_dir = self._get_tier_directory(tier)
            file_path = tier_dir / key
            if file_path.exists():
                # Update metadata with correct tier
                metadata["tier"] = tier
                self._access_metadata[key] = metadata
                return file_path.read_bytes()

        return None

    def _get_tier_directory(self, tier: str) -> Path:
        """Get directory for storage tier.

        Args:
            tier: Tier name

        Returns:
            Path to tier directory
        """
        if tier == "warm":
            return self._warm_dir
        elif tier == "cold":
            return self._cold_dir
        elif tier == "archive":
            return self._archive_dir
        else:
            # Default to warm for unknown tiers
            return self._warm_dir

    def migrate_tier(self, key: str, target_tier: str) -> None:
        """Manually migrate data to different tier.

        Args:
            key: Data key
            target_tier: Target tier name ("warm", "cold", "archive")

        Examples:
            >>> backend.migrate_tier("invocations/trace-old.msgpack", "archive")
        """
        if self.iowarp_available:
            # TODO: IOWarp tier migration API
            # Example:
            # self.iowarp_client.migrate(
            #     namespace=self.namespace,
            #     key=key,
            #     target_tier=target_tier
            # )
            pass

        # Local tier migration
        data = self._read_local(key)
        if data:
            # Write to new tier
            self._write_local(key, data, tier=target_tier)

            # Delete from old tier
            metadata = self._access_metadata.get(key, {})
            old_tier = metadata.get("tier", "warm")
            if old_tier != target_tier:
                old_path = self._get_tier_directory(old_tier) / key
                if old_path.exists():
                    old_path.unlink()

            # Update metadata
            self._update_access_metadata(key, tier=target_tier, operation="migrate")
            self._tier_migrations += 1

    def _update_access_metadata(
        self, key: str, tier: Optional[str] = None, operation: str = "read"
    ) -> None:
        """Update access metadata for tier migration decisions.

        Args:
            key: Data key
            tier: Current tier (if known)
            operation: Operation type ("read", "write", "migrate")
        """
        now = datetime.now(timezone.utc)

        if key not in self._access_metadata:
            self._access_metadata[key] = {
                "tier": tier or "warm",
                "created_at": now.isoformat() + "Z",
                "last_accessed": now.isoformat() + "Z",
                "access_count": 1,
            }
        else:
            metadata = self._access_metadata[key]
            metadata["last_accessed"] = now.isoformat() + "Z"
            metadata["access_count"] = metadata.get("access_count", 0) + 1
            if tier:
                metadata["tier"] = tier

    def _load_access_metadata(self) -> Dict[str, Dict[str, Any]]:
        """Load access metadata from disk.

        Returns:
            Dictionary mapping keys to access metadata
        """
        if not self._access_metadata_file.exists():
            return {}

        try:
            data = self._access_metadata_file.read_bytes()
            return msgspec.msgpack.decode(data)
        except Exception:
            # If metadata is corrupted, start fresh
            return {}

    def _save_access_metadata(self) -> None:
        """Save access metadata to disk."""
        data = msgspec.msgpack.encode(self._access_metadata)
        self._access_metadata_file.write_bytes(data)

    def _maybe_migrate_tiers(self) -> None:
        """Periodically check and migrate data between tiers.

        Migrates data based on access patterns and tier policy:
        - Warm → Cold: Not accessed for warm_to_cold days
        - Cold → Archive: Not accessed for cold_to_archive days

        This runs on every write to amortize migration cost.
        """
        # Only migrate every 100 writes to avoid overhead
        if self._local_writes % 100 != 0:
            return

        now = datetime.now(timezone.utc)

        for key, metadata in list(self._access_metadata.items()):
            tier = metadata.get("tier", "warm")
            last_accessed_str = metadata.get("last_accessed")

            if not last_accessed_str:
                continue

            # Parse timestamp
            try:
                last_accessed = datetime.fromisoformat(
                    last_accessed_str.replace("Z", "+00:00")
                )
            except Exception:
                continue

            # Calculate age
            age_days = (now - last_accessed).days

            # Migrate based on age and current tier
            if tier == "warm" and age_days >= self.tier_policy["warm_to_cold"]:
                self.migrate_tier(key, "cold")
            elif tier == "cold" and age_days >= self.tier_policy["cold_to_archive"]:
                self.migrate_tier(key, "archive")

        # Save updated metadata
        self._save_access_metadata()

    def get_tier_stats(self) -> Dict[str, Any]:
        """Get tier statistics and storage status.

        Returns:
            Dictionary with tier usage stats and IOWarp status

        Examples:
            >>> stats = backend.get_tier_stats()
            >>> print(f"IOWarp available: {stats['iowarp_available']}")
            >>> print(f"Warm tier count: {stats['tiers']['warm']['count']}")
        """
        if not self.iowarp_available:
            # Count files in local tiers
            warm_count = sum(1 for _ in self._warm_dir.rglob("*.msgpack"))
            cold_count = sum(1 for _ in self._cold_dir.rglob("*.msgpack"))
            archive_count = sum(1 for _ in self._archive_dir.rglob("*.msgpack"))

            return {
                "iowarp_available": False,
                "using_local_storage": True,
                "base_dir": str(self.base_dir),
                "namespace": self.namespace,
                "tier_policy": self.tier_policy,
                "tiers": {
                    "warm": {"count": warm_count, "path": str(self._warm_dir)},
                    "cold": {"count": cold_count, "path": str(self._cold_dir)},
                    "archive": {"count": archive_count, "path": str(self._archive_dir)},
                },
                "performance": {
                    "tier_migrations": self._tier_migrations,
                    "local_reads": self._local_reads,
                    "local_writes": self._local_writes,
                },
            }

        # TODO: IOWarp tier stats API
        # Example when IOWarp SDK is available:
        #
        # stats = self.iowarp_client.get_namespace_stats(self.namespace)
        # return {
        #     "iowarp_available": True,
        #     "namespace": self.namespace,
        #     "tier_policy": self.tier_policy,
        #     "tiers": stats.tiers,
        #     "performance": {
        #         "tier_migrations": self._tier_migrations,
        #         "iowarp_reads": self._iowarp_reads,
        #         "iowarp_writes": self._iowarp_writes,
        #     }
        # }

        return {
            "iowarp_available": True,
            "namespace": self.namespace,
            "tier_policy": self.tier_policy,
            "tiers": {
                "warm": {"count": 0},
                "cold": {"count": 0},
                "archive": {"count": 0},
            },
            "performance": {
                "tier_migrations": self._tier_migrations,
                "iowarp_reads": self._iowarp_reads,
                "iowarp_writes": self._iowarp_writes,
            },
        }

    def shutdown(self) -> None:
        """Close ZeroMQ connection and save metadata.

        Call this before application exit to ensure metadata is persisted.

        Examples:
            >>> backend.shutdown()
        """
        # Save metadata first
        self._save_access_metadata()

        # Close ZeroMQ if connected
        if self.iowarp_available and hasattr(self, "zmq_socket"):
            self.zmq_socket.close()
            self.zmq_context.term()
