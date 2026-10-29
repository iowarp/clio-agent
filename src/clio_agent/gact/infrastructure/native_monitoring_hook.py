"""Installed native database/application lifecycle. No container engine or root access."""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Native installs fetch upstream prebuilt binaries only; nothing is compiled on the host.
# A host these builds do not run on (glibc < 2.34, missing system libraries) is refused
# with the reason, and the container variant is the supported path there.
VALKEY_URL = "https://download.valkey.io/releases/valkey-8.1.3-jammy-x86_64.tar.gz"
VALKEY_SHA = "afbbf9e71f171472679280f9e06a8039cf3f1ac7ab6dff76ef3094852c7a342f"
# theseus-rs/postgresql-binaries: upstream PostgreSQL 17.6 built for x86_64 glibc Linux.
POSTGRES_URL = (
    "https://github.com/theseus-rs/postgresql-binaries/releases/download/17.6.0/"
    "postgresql-17.6.0-x86_64-unknown-linux-gnu.tar.gz"
)
POSTGRES_SHA = "19dbf09f0fc33255ff7596fc3ceb7696647286dd15fdf60f3f65c583aae6ad5d"
NO_NATIVE_BUILD = (
    "CLIO does not compile services; use the container variant or site databases on this host."
)
MONGO_SHA = {
    "ubuntu2004": "df64db0da70f23dcfcdf7e859600b45669f993082d19dc7f08586b66fea9f684",
    "ubuntu2204": "de1f62df4710cf6094c75f7ab160a1818f0cc41bbde763b428582069b9dcfda7",
    "ubuntu2404": "9e6a8332ef0eddb52f7869d4363212b3ca379955fd70d80840d1b7fa6f5cb54b",
    "debian12": "c13e85583fc393e0b9f47911797f4e0f724cd6a563cd719ee3360aa592a96f16",
    "rhel8": "94a5f1bb0ec0bcf2d372e787b6bb39ed8662cc073f31271dc57a0c1c3f803dd2",
    # SLES 15 / openSUSE Leap 15, e.g. HPE Cray OS on ALCF Polaris.
    "suse15": "c18266c2566ab88bad100f1f42e8a2150e3669b70f46e032b0b0f77f9f3edd94",
}
#: os-release IDs of SUSE 15 systems that run MongoDB's ``suse15`` build.
SUSE_IDS = frozenset({"sles", "sled", "sle_hpc", "opensuse-leap"})


def _clio_tool(name: str) -> str | None:
    """CLIO's own tool on this host (``native_service_platform.clio_tool``)."""
    try:
        import clio_native_platform as helper  # noqa: PLC0415 - shipped beside the hook
    except ImportError:
        from clio_agent.gact.infrastructure import (  # noqa: PLC0415
            native_service_platform as helper,
        )
    return helper.clio_tool(name)


def private(root: Path, manifest: dict[str, Any]) -> dict[str, str]:
    """Read host-private credentials; never return them through lifecycle receipts."""
    path = root / "credentials.json"
    managed = root / "managed-credentials.json"
    if path.is_symlink() or managed.is_symlink():
        raise ValueError("Native database credentials cannot be a symlink")
    if manifest["database_mode"] == "site":
        source = Path(manifest["database_settings_path"])
        if (
            not source.is_file()
            or source.is_symlink()
            or stat.S_IMODE(source.stat().st_mode) & 0o077
        ):
            raise ValueError("Site database settings must be a private regular file (mode 0600)")
        values = json.loads(source.read_text(encoding="utf-8"))
        required = (
            ("redis_url", "mongodb_uri")
            if manifest["service"] == "flowcept"
            else (
                "POSTGRES_HOST",
                "POSTGRES_PORT",
                "POSTGRES_USER",
                "POSTGRES_PASSWORD",
                "POSTGRES_DB",
            )
        )
        if any(not isinstance(values.get(key), str) or not values[key] for key in required):
            raise ValueError("Site database settings are missing required connection values")
        # Retain a private redaction inventory for the common supervisor's log pump.
        values = {key: values[key] for key in required}
    elif managed.exists():
        values = json.loads(managed.read_text(encoding="utf-8"))
    elif path.exists() and "password" in json.loads(path.read_text(encoding="utf-8")):
        values = json.loads(path.read_text(encoding="utf-8"))
    else:
        values = {"password": secrets.token_urlsafe(32)}
    if manifest["database_mode"] == "managed":
        password = values["password"]
        values = {
            "password": password,
            "redis_url": f"redis://:{password}@127.0.0.1:{manifest['redis_port']}/0",
            "mongodb_uri": f"mongodb://clio:{password}@127.0.0.1:{manifest['mongo_port']}/?authSource=admin",
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": str(manifest["postgres_port"]),
            "POSTGRES_USER": "clio",
            "POSTGRES_PASSWORD": password,
            "POSTGRES_DB": "clio",
        }
        managed.write_text(json.dumps(values), encoding="utf-8")
        managed.chmod(0o600)
    path.write_text(json.dumps(values), encoding="utf-8")
    path.chmod(0o600)
    return values


def flowcept_environment(root: Path) -> None:
    """Point real Flowcept producers and the collector at this installation's settings."""
    os.environ["FLOWCEPT_SETTINGS_PATH"] = str(root / "settings.yaml")


def flowcept_settings(root: Path, credentials: dict[str, str]) -> None:
    """Configure one real Redis transport, MongoDB store and collector."""
    settings = {
        "project": {"db_flush_mode": "online"},
        "log": {
            "log_path": str(root / "logs/flowcept.log"),
            "log_file_level": "error",
            "log_stream_level": "disable",
        },
        "mq": {
            "enabled": True,
            "type": "redis",
            "uri": credentials["redis_url"],
            "channel": "clio-" + json.loads((root / "owner.json").read_text())["owner"][:12],
            "buffer_size": 1,
            "insertion_buffer_time_secs": 0.5,
        },
        "kv_db": {"enabled": True, "uri": credentials["redis_url"]},
        "databases": {
            "mongodb": {"enabled": True, "uri": credentials["mongodb_uri"], "db": "clio"},
            "lmdb": {"enabled": False},
        },
        "db_buffer": {"buffer_size": 1, "insertion_buffer_time_secs": 0.5},
        "agent": {"enabled": False, "chat_enabled": False, "start_persistence": False},
        "web_server": {"dashboards_dir": str(root / "evidence/dashboards")},
    }
    path = root / "settings.yaml"
    if path.is_symlink():
        raise ValueError("Flowcept settings cannot be a symlink")
    path.write_text(json.dumps(settings), encoding="utf-8")
    path.chmod(0o600)


def unpack(root: Path, name: str, url: str, checksum: str) -> Path:
    """Extract one pinned upstream source/binary into an owned installer directory."""
    from native_archive import download, extract  # type: ignore[import-not-found]  # noqa: PLC0415

    staging = root / "tmp" / name
    if staging.exists():
        if staging.is_symlink() or not staging.resolve().is_relative_to(root.resolve()):
            raise ValueError("Unsafe native database staging directory")
        shutil.rmtree(staging)
    return extract(download(url, checksum, root / "cache/downloads"), staging)


def install(root: Path, manifest: dict[str, Any]) -> None:
    """Install prebuilt private databases or validate a site settings file; install real CMF."""
    credentials = private(root, manifest)
    binaries = root / "environment/databases"
    binaries.mkdir(exist_ok=True)
    if manifest["database_mode"] == "managed":
        if platform.machine() not in {"x86_64", "AMD64"}:
            raise RuntimeError(
                f"No prebuilt native databases for {platform.machine()}. {NO_NATIVE_BUILD}"
            )
        if manifest["service"] == "flowcept":
            source = unpack(root, "valkey", VALKEY_URL, VALKEY_SHA)
            shutil.copy2(source / "bin/valkey-server", binaries / "valkey-server")
            check_runs(binaries / "valkey-server", "Valkey's prebuilt Linux build")
            distribution = mongo_platform(Path("/etc/os-release").read_text())
            source = unpack(
                root,
                "mongodb",
                f"https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-{distribution}-8.0.14.tgz",
                MONGO_SHA[distribution],
            )
            shutil.copy2(source / "bin/mongod", binaries / "mongod")
            check_mongod(binaries / "mongod", distribution)
        else:
            source = unpack(root, "postgres", POSTGRES_URL, POSTGRES_SHA)
            target = binaries / "postgres"
            if target.exists():
                if target.is_symlink():
                    raise ValueError("Native PostgreSQL directory cannot be a symlink")
                shutil.rmtree(target)
            shutil.copytree(source, target, symlinks=True)
            check_runs(target / "bin/postgres", "PostgreSQL's prebuilt Linux build")
    if manifest["service"] == "flowcept":
        flowcept_settings(root, credentials)
    else:
        install_cmf(root, manifest)


def check_runs(binary: Path, label: str, hints: dict[str, str] | None = None) -> None:
    """Run a fetched binary once; name what the host lacks when it cannot start.

    ``hints`` maps a loader-error fragment (a missing library) to how to provide it.
    """
    try:
        result = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, check=False, timeout=60
        )
    except OSError as error:
        detail = str(error)
    else:
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout).strip()[:400]
    hint = "".join(" " + text for fragment, text in (hints or {}).items() if fragment in detail)
    raise RuntimeError(f"{label} does not run on this host: {detail}.{hint} {NO_NATIVE_BUILD}")


def check_mongod(mongod: Path, distribution: str) -> None:
    """Run the fetched ``mongod`` once, naming SUSE's OpenSSL 1.1 package when it is missing."""
    hints = (
        {
            "libssl.so.1.1": "On SUSE 15 the OpenSSL 1.1 library comes from the libopenssl1_1 package."
        }
        if distribution == "suse15"
        else {}
    )
    check_runs(mongod, f"MongoDB's verified {distribution} build", hints)


def mongo_platform(release: str) -> str:
    """Choose a compatible verified MongoDB build or require site databases."""
    fields = dict(line.split("=", 1) for line in release.splitlines() if "=" in line)
    system = fields.get("ID", "").strip('"')
    version = fields.get("VERSION_ID", "").strip('"')
    selected = {
        ("ubuntu", "20.04"): "ubuntu2004",
        ("ubuntu", "22.04"): "ubuntu2204",
        ("ubuntu", "24.04"): "ubuntu2404",
        ("debian", "12"): "debian12",
    }.get((system, version))
    if system in {"rhel", "rocky", "almalinux", "centos"} and version.split(".")[0] == "8":
        selected = "rhel8"
    if system in SUSE_IDS and version.split(".")[0] == "15":
        selected = "suse15"
    if selected is None:
        raise RuntimeError(
            "No verified prebuilt MongoDB binary for this Linux distribution. " + NO_NATIVE_BUILD
        )
    return selected


def install_cmf(root: Path, manifest: dict[str, Any]) -> None:
    """Install CMF's pinned server lock and rebase its fixed paths into owned data."""
    uv = _clio_tool("uv") or "uv"
    source = root / "environment/cmf-source"
    if source.exists():
        if source.is_symlink():
            raise ValueError("CMF source directory cannot be a symlink")
        shutil.rmtree(source)
    subprocess.run(
        ["git", "clone", "--no-checkout", "https://github.com/HewlettPackard/cmf.git", str(source)],
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "--detach", manifest["cmf_revision"]], cwd=source, check=True
    )
    data = root / "data/cmf"
    (data / "static").mkdir(parents=True, exist_ok=True)
    patches = {}
    for path in source.rglob("*"):
        if path.suffix not in {".py", ".html", ".js"} or not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        revised = original.replace("/cmf-server/data", data.as_posix())
        if revised != original:
            patches[str(path.relative_to(source))] = original.count("/cmf-server/data")
            path.write_text(revised, encoding="utf-8")
    (root / "evidence/cmf-path-adaptation.json").write_text(json.dumps(patches), encoding="utf-8")
    subprocess.run(
        [
            uv,
            "pip",
            "sync",
            "--python",
            sys.executable,
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--no-binary",
            "antlr4-python3-runtime",
            str(root / "cmf-server.lock"),
        ],
        check=True,
    )
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            "--no-build-isolation",
            str(source),
        ],
        check=True,
    )


def wait_port(process: subprocess.Popen[Any], port: int) -> None:
    """Wait for a real owned database listener, failing when its process exits."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("Native database did not start; inspect its retained logs")


@contextmanager
def databases(root: Path, manifest: dict[str, Any], credentials: dict[str, str]) -> Iterator[None]:
    """Supervise native children in the owning worker's group; never daemonize or touch site DBs."""
    children: list[subprocess.Popen[Any]] = []
    logs = []
    try:
        if manifest["database_mode"] == "managed":
            binaries = root / "environment/databases"
            data = root / "data"
            data.mkdir(exist_ok=True)
            commands = []
            if manifest["service"] == "flowcept":
                redis = data / "redis"
                mongo = data / "mongo"
                redis.mkdir(exist_ok=True)
                mongo.mkdir(exist_ok=True)
                config = root / "redis.conf"
                config.write_text(
                    f"bind 127.0.0.1\nport {manifest['redis_port']}\nprotected-mode yes\nrequirepass {credentials['password']}\nappendonly yes\ndir {redis}\n",
                    encoding="utf-8",
                )
                config.chmod(0o600)
                commands = [
                    (
                        "redis",
                        [str(binaries / "valkey-server"), str(config)],
                        manifest["redis_port"],
                    ),
                    (
                        "mongo",
                        [
                            str(binaries / "mongod"),
                            "--bind_ip",
                            "127.0.0.1",
                            "--port",
                            str(manifest["mongo_port"]),
                            "--dbpath",
                            str(mongo),
                            "--auth",
                            "--wiredTigerCacheSizeGB",
                            "0.25",
                        ],
                        manifest["mongo_port"],
                    ),
                ]
            else:
                pg = binaries / "postgres/bin"
                database = data / "postgres"
                if not (database / "PG_VERSION").exists():
                    password = root / "pg-password"
                    if password.is_symlink():
                        raise ValueError("PostgreSQL initialization password cannot be a symlink")
                    password.write_text(credentials["POSTGRES_PASSWORD"], encoding="utf-8")
                    password.chmod(0o600)
                    try:
                        subprocess.run(
                            [
                                str(pg / "initdb"),
                                "-D",
                                str(database),
                                "-U",
                                "clio",
                                "--auth-host=scram-sha-256",
                                "--auth-local=scram-sha-256",
                                "--pwfile=" + str(password),
                            ],
                            check=True,
                        )
                    finally:
                        password.unlink(missing_ok=True)
                commands = [
                    (
                        "postgres",
                        [
                            str(pg / "postgres"),
                            "-D",
                            str(database),
                            "-h",
                            "127.0.0.1",
                            "-p",
                            str(manifest["postgres_port"]),
                            "-k",
                            str(root / "tmp"),
                        ],
                        manifest["postgres_port"],
                    )
                ]
            for role, command, port in commands:
                with socket.socket() as listener:
                    listener.settimeout(0.5)
                    if listener.connect_ex(("127.0.0.1", port)) == 0:
                        raise RuntimeError(f"Native {role} port {port} already has a listener")
                output = (root / "logs" / f"{role}.log").open("a", encoding="utf-8")
                logs.append(output)
                process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
                children.append(process)
                wait_port(process, port)
            if manifest["service"] == "flowcept":
                from pymongo import MongoClient  # type: ignore[import-untyped]  # noqa: PLC0415
                from pymongo.errors import OperationFailure  # noqa: PLC0415

                client: MongoClient[dict[str, Any]]
                try:
                    with MongoClient(
                        credentials["mongodb_uri"], serverSelectionTimeoutMS=5000
                    ) as client:
                        client.admin.command("ping")
                except OperationFailure as error:
                    # Only MongoDB's initial local-user bootstrap is allowed.
                    if error.code not in {13, 18}:
                        raise
                    with MongoClient("127.0.0.1", manifest["mongo_port"]) as client:
                        client.admin.command(
                            "createUser", "clio", pwd=credentials["password"], roles=["root"]
                        )
            else:
                env = {**os.environ, "PGPASSWORD": credentials["POSTGRES_PASSWORD"]}
                pg = binaries / "postgres/bin"
                common = ["-h", "127.0.0.1", "-p", str(manifest["postgres_port"]), "-U", "clio"]
                deadline = time.monotonic() + 90
                while True:
                    ready = subprocess.run(
                        [str(pg / "pg_isready"), *common, "-t", "1"],
                        capture_output=True,
                        env=env,
                        check=False,
                    )
                    if ready.returncode == 0:
                        break
                    if time.monotonic() >= deadline or any(
                        child.poll() is not None for child in children
                    ):
                        raise RuntimeError(
                            "Native PostgreSQL did not finish recovery; inspect postgres.log"
                        )
                    time.sleep(0.2)
                exists = subprocess.run(
                    [
                        str(pg / "psql"),
                        *common,
                        "-d",
                        "postgres",
                        "-tAc",
                        "SELECT 1 FROM pg_database WHERE datname='clio'",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                if exists.stdout.strip() != "1":
                    subprocess.run([str(pg / "createdb"), *common, "clio"], env=env, check=True)
        yield
        if any(process.poll() is not None for process in children):
            raise RuntimeError("An owned native database exited; inspect its logs")
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()


def serve(root: Path, manifest: dict[str, Any]) -> None:
    """Serve the real Flowcept collector/API or CMF application over native databases."""
    import uvicorn  # noqa: PLC0415

    credentials = private(root, manifest)
    with databases(root, manifest, credentials):
        if manifest["service"] == "flowcept":
            flowcept_settings(root, credentials)
            flowcept_environment(root)
            from flowcept.flowceptor.consumers.document_inserter import (
                DocumentInserter,  # type: ignore[import-not-found]  # noqa: PLC0415
            )
            from flowcept.webservice.main import (
                app,  # type: ignore[import-not-found]  # noqa: PLC0415
            )

            owner = json.loads((root / "owner.json").read_text())["owner"]
            collector = DocumentInserter(check_safe_stops=False, bundle_exec_id=owner)
            collector.start(threaded=True)
            try:
                uvicorn.run(app, host="127.0.0.1", port=manifest["port"], log_level="warning")
            finally:
                collector.stop(bundle_exec_id=owner)
        else:
            os.environ.update(
                {key: value for key, value in credentials.items() if key.startswith("POSTGRES_")}
            )
            os.environ.update(
                HOME=str(root / "data/cmf"), CMF_LOG_FILE=str(root / "logs/cmflib.log")
            )
            source = root / "environment/cmf-source"
            sys.path.insert(0, str(source))
            uvicorn.run(
                "server.app.main:app", host="127.0.0.1", port=manifest["port"], log_level="warning"
            )


def main() -> None:
    """Dispatch definition-owned install, launch and database-health actions."""
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    action = sys.argv[1]
    if action == "install":
        install(root, manifest)
    elif action == "serve":
        serve(root, manifest)
    elif action == "running":
        credentials = private(root, manifest)
        if manifest["service"] == "flowcept":
            import redis  # noqa: PLC0415
            from pymongo import MongoClient  # type: ignore[import-untyped]  # noqa: PLC0415

            with redis.Redis.from_url(credentials["redis_url"]) as task_backend:
                task_backend.execute_command("PING")
            client: MongoClient[dict[str, Any]]
            with MongoClient(credentials["mongodb_uri"], serverSelectionTimeoutMS=5000) as client:
                client.admin.command("ping")
        # CMF's readiness only succeeds after its PostgreSQL-backed lifespan initializes.
    else:
        raise ValueError("Unsupported native monitoring lifecycle action")


if __name__ == "__main__":
    main()
