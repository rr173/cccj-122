"""Domain service: register taxonomy packages, resolve entries, verify offline.

All operations go through :class:`TaxonomyRegistry`, which coordinates the
strict ZIP reader, the OASIS catalog rules, the dependency resolver, the
immutable on-disk store and each package's SQLite catalog. Nothing here opens
a network socket or touches a fact warehouse.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from .catalogdb import PackageCatalog
from .errors import (
    InvocationError,
    PackageFormatError,
    RegistryCorruptionError,
    VersionConflictError,
)
from .models import (
    REGISTRY_SCHEMA_VERSION,
    CatalogRule,
    PackageMember,
    RegisterResult,
    ResolutionBatch,
    VerifyReport,
)
from .resolver import SchemaResolver
from .store import TaxonomyStore, check_digest_value
from .zippkg import load_package_zip


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TaxonomyRegistry:
    """All package/entry operations against one registry directory."""

    def __init__(self, root: str, *, create: bool = False):
        self.store = TaxonomyStore(root)
        if create:
            self.store.ensure_layout()
        else:
            self.store.check_layout()

    # -- registration -----------------------------------------------------------

    def register_zip(self, zip_path: str) -> RegisterResult:
        """Register one taxonomy package ZIP; idempotent by package SHA-256."""
        loaded, payloads = load_package_zip(zip_path)
        digest = loaded.package_sha256
        with self.store.locked(exclusive=True):
            leftovers = self.store.check_layout()
            if leftovers:
                raise RegistryCorruptionError(
                    "leftover staging directories from a failed publish exist:"
                    f" {sorted(leftovers)}; refusing to register until removed")
            if self.store.package_exists(digest):
                existing = self._read_identity(digest)
                return RegisterResult(
                    package_sha256=digest,
                    identifier=existing["identifier"],
                    version=existing["version"],
                    reused=True,
                    package_dir=str(self.store.package_dir(digest)),
                    member_count=existing["member_count"],
                    entry_count=existing["entry_count"],
                )
            self._reject_version_conflict(loaded.descriptor.identifier,
                                          loaded.descriptor.version)
            return self._publish_new(loaded, payloads)

    def _reject_version_conflict(self, identifier: str, version: str) -> None:
        for existing_digest in self.store.list_package_dirs():
            meta = self.store.load_meta(existing_digest)
            if meta.get("identifier") == identifier and \
                    meta.get("version") == version:
                raise VersionConflictError(
                    f"package {identifier!r} version {version!r} is already"
                    f" registered as {existing_digest}; different content with"
                    " the same identifier+version is refused (the existing"
                    " package is never overwritten)")

    def _publish_new(self, loaded, payloads) -> RegisterResult:
        digest = loaded.package_sha256
        staging = self.store.create_staging(digest)
        try:
            # 1. content-addressed member blobs
            for member in loaded.members:
                self.store.write_blob(staging, member.sha256,
                                      payloads[member.path])
            # 2. SQLite catalog, fully built inside the staging directory
            db_path = str(staging / "catalog.sqlite")
            entry_logical_uris = loaded.entry_logical_uris
            with PackageCatalog(db_path, writable=True, create=True) as catalog:
                catalog.write_meta_fields({
                    "package_sha256": digest,
                    "identifier": loaded.descriptor.identifier,
                    "name": loaded.descriptor.name,
                    "version": loaded.descriptor.version,
                    "top_level_dir": loaded.top_level_dir,
                    "original_zip_sha256": digest,
                    "registered_at": utcnow(),
                    "schema_version": str(REGISTRY_SCHEMA_VERSION),
                })
                catalog.write_members(loaded.members)
                catalog.write_rules(loaded.catalog_rules)
                catalog.write_entries(loaded.descriptor.entry_points,
                                      loaded.entry_member_paths,
                                      entry_logical_uris)
            # 3. human-readable metadata sidecar
            self.store.write_meta(staging, {
                "package_sha256": digest,
                "identifier": loaded.descriptor.identifier,
                "name": loaded.descriptor.name,
                "version": loaded.descriptor.version,
                "top_level_dir": loaded.top_level_dir,
                "member_count": len(loaded.members),
                "entry_count": len(loaded.descriptor.entry_points),
                "registered_at": utcnow(),
                "schema_version": REGISTRY_SCHEMA_VERSION,
            })
            # 4. atomic directory publish; failure leaves no valid package
            self.store.publish_staging(staging, digest)
        except BaseException:
            self.store.remove_staging(staging)
            raise
        return RegisterResult(
            package_sha256=digest,
            identifier=loaded.descriptor.identifier,
            version=loaded.descriptor.version,
            reused=False,
            package_dir=str(self.store.package_dir(digest)),
            member_count=len(loaded.members),
            entry_count=len(loaded.descriptor.entry_points),
        )

    # -- resolution --------------------------------------------------------------

    def resolve_entries(self, package_digest: str,
                        entry_uris: Optional[tuple[str, ...]] = None
                        ) -> ResolutionBatch:
        """Resolve one entry set; same input always returns the same batch.

        A fast shared-lock read returns an existing batch without writing
        anything. When a new batch is needed the whole resolve + publish runs
        under the exclusive lock and the catalog file is replaced
        atomically (staging copy -> rename), so a reader never sees a
        half-written catalog and no WAL/journal side cars survive.
        """
        check_digest_value(package_digest)
        if not self.store.package_exists(package_digest):
            raise InvocationError(
                f"no package {package_digest} in registry {self.store.root}")
        with self.store.locked(exclusive=False):
            self._reject_leftovers()
            chosen = self._chosen_entries(package_digest, entry_uris)
            entry_set_hash = self._entry_set_hash(chosen)
            existing = self._find_existing_batch(package_digest, entry_set_hash)
            if existing is not None:
                return existing
        with self.store.locked(exclusive=True):
            self._reject_leftovers()
            # Re-check under the exclusive lock: another process may have
            # published the identical entry set while we waited.
            existing = self._find_existing_batch(package_digest, entry_set_hash)
            if existing is not None:
                return existing
            return self._build_and_publish_batch(
                package_digest, entry_set_hash, chosen)

    def _chosen_entries(self, package_digest, entry_uris):
        with PackageCatalog(
                str(self.store.package_db_path(package_digest)),
                writable=False) as catalog:
            declared = tuple(
                row["logical_uri"] for row in catalog.entry_rows())
        chosen = declared if entry_uris is None \
            else self._validate_chosen_entries(entry_uris, declared)
        return tuple(sorted(set(chosen)))

    def _find_existing_batch(self, package_digest: str,
                             entry_set_hash: str) -> Optional[ResolutionBatch]:
        with PackageCatalog(
                str(self.store.package_db_path(package_digest)),
                writable=False) as catalog:
            row = catalog.find_batch(entry_set_hash)
            return catalog.load_batch(row["batch_id"]) if row else None

    def _build_and_publish_batch(self, package_digest, entry_set_hash, chosen):
        with PackageCatalog(
                str(self.store.package_db_path(package_digest)),
                writable=False) as catalog:
            files, edges = self._compute_closure(
                package_digest, catalog, chosen)
        self.store.atomic_replace_catalog(
            package_digest,
            lambda staging_db: self._write_batch_into(
                staging_db, entry_set_hash, chosen, files, edges))
        return self._find_existing_batch(package_digest, entry_set_hash)

    def _write_batch_into(self, staging_db_path: str, entry_set_hash: str,
                          chosen, files, edges) -> None:
        with PackageCatalog(staging_db_path, writable=True) as catalog:
            with catalog.transaction():
                catalog.insert_batch(
                    entry_set_hash=entry_set_hash,
                    entry_uris=chosen,
                    files=files,
                    edges=edges,
                    created_at=utcnow())

    def _compute_closure(self, package_digest, catalog, chosen):
        member_rows = catalog.member_rows()
        rule_rows = catalog.rule_rows()
        members = tuple(
            PackageMember(
                path=row["member_path"],
                sha256=row["blob_digest"],
                byte_size=row["byte_size"],
                is_xml=bool(row["is_xml"]),
            ) for row in member_rows)
        rules = tuple(
            CatalogRule(
                uri_start_string=row["uri_start_string"],
                rewrite_prefix=row["rewrite_prefix"],
                rewrite_prefix_raw=row["rewrite_prefix_raw"],
                line=row["line"],
            ) for row in rule_rows)
        blob_by_path = {row["member_path"]: row["blob_digest"]
                        for row in member_rows}

        def load_payload(member_path: str) -> bytes:
            # Only closure members are ever fetched; unrelated ZIP members are
            # never parsed as schemas.
            return self.store.read_member_bytes(
                package_digest, blob_by_path[member_path])

        class _LazyPayloads(dict):
            def __missing__(self, key):
                value = load_payload(key)
                self[key] = value
                return value

        resolver = SchemaResolver(
            package_sha256=package_digest,
            members=members,
            catalog_rules=rules,
            payloads=_LazyPayloads())
        return resolver.resolve(chosen)

    @staticmethod
    def _validate_chosen_entries(requested, declared):
        chosen = []
        declared_set = set(declared)
        for uri in requested:
            if uri not in declared_set:
                raise InvocationError(
                    f"entry {uri!r} is not declared by this package;"
                    f" declared entries: {sorted(declared_set)}")
            chosen.append(uri)
        return tuple(chosen)

    @staticmethod
    def _entry_set_hash(entry_uris: tuple[str, ...]) -> str:
        joined = "\n".join(sorted(set(entry_uris)))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    # -- inspection --------------------------------------------------------------

    def list_packages(self) -> list[dict]:
        with self.store.locked(exclusive=False):
            self._reject_leftovers()
            result = []
            for digest in self.store.list_package_dirs():
                result.append(self.store.load_meta(digest))
            return result

    def show_package(self, package_digest: str) -> dict:
        check_digest_value(package_digest)
        if not self.store.package_exists(package_digest):
            raise InvocationError(f"no package {package_digest} in registry")
        with self.store.locked(exclusive=False):
            meta = self.store.load_meta(package_digest)
            with PackageCatalog(
                    str(self.store.package_db_path(package_digest)),
                    writable=False) as catalog:
                return {
                    "meta": meta,
                    "entries": [dict(row) for row in catalog.entry_rows()],
                    "catalog_rules": [dict(row)
                                      for row in catalog.rule_rows()],
                    "members": [dict(row) for row in catalog.member_rows()],
                    "batches": [dict(row) for row in catalog.batch_rows()],
                }

    def load_batch(self, package_digest: str, batch_id: int) -> ResolutionBatch:
        check_digest_value(package_digest)
        with self.store.locked(exclusive=False):
            if not self.store.package_exists(package_digest):
                raise InvocationError(f"no package {package_digest} in registry")
            with PackageCatalog(
                    str(self.store.package_db_path(package_digest)),
                    writable=False) as catalog:
                return catalog.load_batch(batch_id)

    def latest_batch(self, package_digest: str) -> Optional[ResolutionBatch]:
        detail = self.show_package(package_digest)
        batches = detail["batches"]
        if not batches:
            return None
        return self.load_batch(package_digest, max(b["batch_id"] for b in batches))

    def query_uri(self, package_digest: str, uri: str) -> dict:
        """Look up a public URI, a logical URI, an original href or a target."""
        check_digest_value(package_digest)
        with self.store.locked(exclusive=False):
            with PackageCatalog(
                    str(self.store.package_db_path(package_digest)),
                    writable=False) as catalog:
                member = catalog.find_member(uri)
                resolved_member_path = None
                if member is None:
                    # A logical xbrl-pkg URI identifies one member directly.
                    from .uriutil import (
                        is_logical_uri,
                        logical_uri_to_member_path,
                        strip_fragment,
                    )
                    if is_logical_uri(uri):
                        try:
                            resolved_member_path = logical_uri_to_member_path(
                                strip_fragment(uri), where="query")
                            member = catalog.find_member(resolved_member_path)
                        except PackageFormatError:
                            member = None
                files = catalog.find_resolution_files(uri)
                # A public pre-rewrite URI or a raw relative href is stored on
                # the edge, not on the file row; follow matching edges to the
                # files they resolved to so all three dimensions are queryable.
                direct_keys = {(r["batch_id"], r["public_uri"]) for r in files}
                for row in catalog.find_resolution_files_via_edges(uri):
                    if (row["batch_id"], row["public_uri"]) not in direct_keys:
                        files.append(row)
                        direct_keys.add((row["batch_id"], row["public_uri"]))
                edges = catalog.find_edges(uri)
                return {
                    "query": uri,
                    "resolved_member_path": resolved_member_path,
                    "member": dict(member) if member is not None else None,
                    "resolution_files": [dict(r) for r in files],
                    "edges": [dict(r) for r in edges],
                }

    # -- verification -------------------------------------------------------------

    def verify(self) -> VerifyReport:
        """Full offline verification: layout, blobs, digests, resolution tables."""
        problems: list[str] = []
        members_verified = 0
        bytes_verified = 0
        batches_verified = 0
        with self.store.locked(exclusive=False):
            try:
                leftovers = self.store.check_layout()
            except RegistryCorruptionError as exc:
                return VerifyReport(False, 0, 0, 0, 0, (str(exc),))
            if leftovers:
                problems.append(
                    f"leftover staging directories: {sorted(leftovers)}")
            for digest in self.store.list_package_dirs():
                try:
                    counts = self._verify_package(digest)
                except RegistryCorruptionError as exc:
                    problems.append(str(exc))
                    continue
                members_verified += counts[0]
                bytes_verified += counts[1]
                batches_verified += counts[2]
        return VerifyReport(
            ok=not problems,
            package_count=len(self.store.list_package_dirs()),
            members_verified=members_verified,
            bytes_verified=bytes_verified,
            batches_verified=batches_verified,
            problems=tuple(problems),
        )

    def _verify_package(self, digest: str) -> tuple[int, int, int]:
        # 1. meta sidecar matches the SQLite catalog metadata
        meta = self.store.load_meta(digest)
        with PackageCatalog(
                str(self.store.package_db_path(digest)),
                writable=False) as catalog:
            db_meta = catalog.meta_fields()
            for key in ("identifier", "name", "version", "package_sha256"):
                if meta.get(key) != db_meta.get(key):
                    raise RegistryCorruptionError(
                        f"package {digest}: meta.json {key}={meta.get(key)!r}"
                        f" disagrees with catalog {db_meta.get(key)!r}")
            if db_meta.get("package_sha256") != digest:
                raise RegistryCorruptionError(
                    f"package {digest}: catalog digest does not match directory"
                    " name")

            member_rows = catalog.member_rows()
            blob_paths = set(
                str(p) for p in self.store.iter_blob_paths(digest))
            referenced_blobs: set[str] = set()
            for row in member_rows:
                blob_path = str(self.store.blob_path(digest, row["blob_digest"]))
                referenced_blobs.add(blob_path)
                if blob_path not in blob_paths:
                    raise RegistryCorruptionError(
                        f"package {digest}: member {row['member_path']!r}"
                        f" references missing blob {row['blob_digest']}")
                data = self.store.read_member_bytes(digest, row["blob_digest"])
                actual = hashlib.sha256(data).hexdigest()
                if actual != row["blob_digest"]:
                    raise RegistryCorruptionError(
                        f"package {digest}: member {row['member_path']!r}"
                        f" bytes changed; expected {row['blob_digest']},"
                        f" found {actual}")
                if len(data) != row["byte_size"]:
                    raise RegistryCorruptionError(
                        f"package {digest}: member {row['member_path']!r}"
                        f" size mismatch ({len(data)} vs {row['byte_size']})")
            orphans = blob_paths - referenced_blobs
            if orphans:
                raise RegistryCorruptionError(
                    f"package {digest}: {len(orphans)} blob file(s) on disk are"
                    " not referenced by the catalog")

            # 2. every batch's files/edges still resolve consistently
            for batch_row in catalog.batch_rows():
                batch = catalog.load_batch(batch_row["batch_id"])
                member_digest = {row["member_path"]: row["blob_digest"]
                                 for row in member_rows}
                for f in batch.files:
                    if f.member_path not in member_digest:
                        raise RegistryCorruptionError(
                            f"package {digest} batch {batch.batch_id}: file"
                            f" {f.public_uri!r} points to unknown member"
                            f" {f.member_path!r}")
                    if member_digest[f.member_path] != f.member_sha256:
                        raise RegistryCorruptionError(
                            f"package {digest} batch {batch.batch_id}: digest of"
                            f" {f.member_path!r} no longer matches the"
                            " resolution table")
                targets = {f.public_uri for f in batch.files}
                for edge in batch.edges:
                    if edge.source_uri not in targets \
                            or edge.target_uri not in targets:
                        raise RegistryCorruptionError(
                            f"package {digest} batch {batch.batch_id}: edge"
                            f" {edge.source_uri!r} -> {edge.target_uri!r}"
                            " references a URI absent from the resolution set")
                if batch.file_count != len(batch.files) \
                        or batch.edge_count != len(batch.edges):
                    raise RegistryCorruptionError(
                        f"package {digest} batch {batch.batch_id}: stored counts"
                        " do not match table contents")
            batches = len(catalog.batch_rows())
        return len(member_rows), sum(r["byte_size"] for r in member_rows), batches

    def _reject_leftovers(self) -> None:
        leftovers = [
            entry.name for entry in self.store.packages_dir.iterdir()
            if ".staging-" in entry.name]
        if leftovers:
            raise RegistryCorruptionError(
                f"leftover staging directories from failed publish: {leftovers}")

    def _read_identity(self, digest: str) -> dict:
        meta = self.store.load_meta(digest)
        return {
            "identifier": meta["identifier"],
            "version": meta["version"],
            "member_count": meta["member_count"],
            "entry_count": meta["entry_count"],
        }
