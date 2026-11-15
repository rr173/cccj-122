"""URI helpers: logical package URIs, catalog rewrites and containment checks.

The resolver works entirely on URIs. A member's *logical URI* uses the private
``xbrl-pkg`` scheme; public URIs (typically ``http(s)``) are only turned into
package paths after a catalog ``rewriteURI`` rule matches. Nothing here ever
performs a network lookup and nothing treats a URI string as a local filename.
"""
from __future__ import annotations

import posixpath
import re
from urllib.parse import quote, unquote, urljoin, urlsplit

from .errors import PackageFormatError, ResolutionError
from .models import PKG_SCHEME, PKG_SCHEME_PREFIX

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_PKG_NETLOC = "package"


def member_logical_uri(member_path: str) -> str:
    """Canonical logical URI of an in-package member path."""
    quoted = quote(member_path, safe="/")
    return f"{PKG_SCHEME_PREFIX}{_PKG_NETLOC}/{quoted}"


def is_logical_uri(uri: str) -> bool:
    try:
        return urlsplit(uri).scheme == PKG_SCHEME
    except ValueError:
        return False


def join_uri(base: str, ref: str) -> str:
    """Resolve ``ref`` against ``base`` without touching the filesystem.

    Public ``http(s)`` style URIs follow RFC 3986 via :func:`urljoin`. The
    private ``xbrl-pkg`` scheme is opaque to ``urljoin``, so relative
    references against a logical package URI are resolved with POSIX path
    rules on the percent-encoded path and re-emitted as a logical URI.
    """
    if is_logical_uri(base):
        if "://" in ref and urlsplit(ref).scheme:
            return ref  # already absolute
        ref_no_frag, fragment = _split_fragment(ref)
        parts = urlsplit(base)
        base_path = parts.path
        if ref_no_frag.startswith("/"):
            target_path = _normpath_keep_slash(ref_no_frag)
        else:
            # A trailing slash marks a directory; without one, the last
            # component is a file and is replaced by the reference.
            if base_path.endswith("/"):
                directory = base_path
            else:
                parent = posixpath.dirname(base_path)
                directory = parent if parent.endswith("/") else parent + "/"
            target_path = _normpath_keep_slash(directory + ref_no_frag)
        result = f"{PKG_SCHEME_PREFIX}{parts.netloc}{target_path}"
        return result + fragment if fragment else result
    return urljoin(base, ref)


def _normpath_keep_slash(path: str) -> str:
    """posixpath.normpath that preserves a trailing directory slash.

    ``normpath("parts/")`` returns ``"parts"``, which would wrongly turn a
    directory-style xml:base into a file-style base for the next relative
    reference. The trailing slash is therefore restored after normalising.
    """
    normalised = posixpath.normpath(path)
    if path.endswith("/") and not normalised.endswith("/"):
        return normalised + "/"
    return normalised


def _split_fragment(uri: str) -> tuple[str, str]:
    index = uri.find("#")
    if index == -1:
        return uri, ""
    return uri[:index], uri[index:]


def strip_fragment(uri: str) -> str:
    """File identity ignores the ``#fragment`` part of a reference."""
    index = uri.find("#")
    return uri if index == -1 else uri[:index]


def apply_rewrite(uri: str, uri_start_string: str, rewrite_prefix: str) -> str:
    """Apply one OASIS rewriteURI rule (the prefix match is already known).

    A single boundary slash is kept when the public suffix begins with one,
    so ``prefix=".../schemas/base/"`` + suffix ``"/types.xsd"`` yields
    ``.../schemas/base/types.xsd`` rather than a double slash. A prefix that
    ends at the package root (``xbrl-pkg://package/``) is handled the same
    way, so the root is never rendered as ``package//``.
    """
    suffix = uri[len(uri_start_string):]
    if suffix.startswith("/") and (
            rewrite_prefix.endswith("/") or
            rewrite_prefix.endswith("://")):
        return rewrite_prefix + suffix[1:]
    return rewrite_prefix + suffix


def normalize_member_path(path: str, *, where: str) -> str:
    """Normalize a package-relative path and refuse every escape attempt.

    Percent escapes have already been decoded by the caller. Absolute paths,
    Windows drive paths, backslashes and any ``..`` that would leave the
    package root are refused. The output never starts with ``/``.
    """
    if not path:
        raise ResolutionError(f"{where}: empty member path")
    if "\x00" in path:
        raise PackageFormatError(f"{where}: NUL byte in member path")
    if "\\" in path:
        raise PackageFormatError(
            f"{where}: backslash in member path {path!r}; ZIP paths use '/'")
    if path.startswith("/"):
        raise PackageFormatError(
            f"{where}: absolute member path {path!r} is refused")
    if any(_DRIVE_RE.match(part) for part in path.split("/")):
        raise PackageFormatError(
            f"{where}: drive-letter member path {path!r} is refused")
    normalized = posixpath.normpath(path)
    if normalized == ".." or normalized.startswith("../") or normalized == ".":
        raise PackageFormatError(
            f"{where}: member path {path!r} escapes the package root")
    if any(_DRIVE_RE.match(part) for part in normalized.split("/")) \
            or normalized.startswith("/"):
        raise PackageFormatError(
            f"{where}: member path {path!r} normalises outside the package")
    return normalized


def logical_uri_to_member_path(uri: str, *, where: str) -> str:
    """Map an ``xbrl-pkg`` logical URI back to a contained member path.

    The URI is percent-decoded before normalisation, so an escape hidden as
    ``%2e%2e%2f`` is still caught and can never reach the package root.
    """
    decoded_path = _decoded_pkg_path(uri, where=where)
    if not decoded_path.startswith("/"):
        raise ResolutionError(f"{where}: malformed logical URI {uri!r}")
    return normalize_member_path(decoded_path.lstrip("/"), where=where)


def assert_logical_uri_contained(uri: str, *, where: str) -> None:
    """Validate that a logical URI (e.g. a rewrite prefix) stays in the package.

    The URI may denote a directory, so a trailing slash is tolerated.
    """
    decoded_path = _decoded_pkg_path(uri, where=where)
    candidate = decoded_path.strip("/")
    if not candidate:
        return  # the package root itself is a valid directory target
    normalize_member_path(candidate, where=where)


def _decoded_pkg_path(uri: str, *, where: str) -> str:
    parts = urlsplit(uri)
    if parts.scheme != PKG_SCHEME:
        raise ResolutionError(
            f"{where}: {uri!r} is not an in-package {PKG_SCHEME} URI")
    if parts.netloc != _PKG_NETLOC:
        raise ResolutionError(
            f"{where}: logical URI {uri!r} has unexpected netloc {parts.netloc!r}")
    if parts.query:
        raise ResolutionError(
            f"{where}: logical URI {uri!r} carries a query string; unsupported")
    return unquote(parts.path)


def resolve_relative_member_path(base_member_path: str, ref: str, *,
                                 where: str) -> str:
    """Resolve a package-internal relative reference against a member path."""
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base_member_path),
                                               ref))
    return normalize_member_path(joined, where=where)
