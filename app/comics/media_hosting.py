"""Comic media-hosting providers and strict public URL validation."""

import hashlib
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests

from app.config import (
    COMIC_GITHUB_PAGES_ASSET_PREFIX,
    COMIC_MEDIA_HOSTING_PROVIDER,
    COMIC_PUBLIC_MANIFEST_PATH,
    COMIC_STOCK_MANIFEST_PATH,
    PROJECT_ROOT,
    REQUEST_TIMEOUT,
)


MEDIA_HASH_OK = "MEDIA_HASH_OK"
MEDIA_HASH_MISMATCH = "MEDIA_HASH_MISMATCH"
MEDIA_HOSTING_NOT_CONFIGURED = "MEDIA_HOSTING_NOT_CONFIGURED"
PUBLIC_URL_VALIDATION_FAILED = "PUBLIC_URL_VALIDATION_FAILED"
PUBLIC_COMIC_MANIFEST_INVALID = "PUBLIC_COMIC_MANIFEST_INVALID"
PUBLIC_URL_OK = "PUBLIC_URL_OK"


class MediaHostingError(RuntimeError):
    """A credential-safe media hosting or validation error."""


@dataclass(frozen=True)
class HostedComicAsset:
    comic_id: str
    provider: str
    public_url: str
    sha256: str
    uploaded_at: datetime
    expires_at: Optional[datetime]
    integrity_status: str

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return self.expires_at is not None and self.expires_at <= current


@dataclass(frozen=True)
class PublicMediaValidation:
    public_url: str
    content_type: str
    content_length: int
    sha256: str
    integrity_status: str


@dataclass(frozen=True)
class MediaHostingDetection:
    found: bool
    provider: str
    details: str


@dataclass(frozen=True)
class PublicComicAsset:
    comic_id: str
    file: str
    public_url: str
    sha256: str
    local_path: Path


class ComicMediaHostingProvider:
    name = "abstract"

    def publish_asset(
        self, local_path: str, comic_id: str, sha256: str
    ) -> HostedComicAsset:
        raise NotImplementedError


class DisabledComicMediaHostingProvider(ComicMediaHostingProvider):
    name = "disabled"

    def publish_asset(
        self, local_path: str, comic_id: str, sha256: str
    ) -> HostedComicAsset:
        raise MediaHostingError(MEDIA_HOSTING_NOT_CONFIGURED)


def _resolved_addresses(
    hostname: str,
    resolver: Callable[..., Iterable[tuple]] = socket.getaddrinfo,
) -> Iterable[str]:
    for record in resolver(hostname, 443, type=socket.SOCK_STREAM):
        yield record[4][0]


def _validate_public_host(
    hostname: Optional[str],
    resolver: Callable[..., Iterable[tuple]],
) -> None:
    if not hostname or hostname.lower() == "localhost":
        raise MediaHostingError(PUBLIC_URL_VALIDATION_FAILED + ": localhost")
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [str(literal)]
    except ValueError:
        try:
            addresses = list(_resolved_addresses(hostname, resolver))
        except OSError as error:
            raise MediaHostingError(
                PUBLIC_URL_VALIDATION_FAILED + ": DNS resolution failed"
            ) from error
    if not addresses:
        raise MediaHostingError(PUBLIC_URL_VALIDATION_FAILED + ": no public address")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise MediaHostingError(
                PUBLIC_URL_VALIDATION_FAILED + ": private address"
            )


def validate_public_media_url(
    public_url: str,
    *,
    expected_sha256: Optional[str] = None,
    session: Optional[requests.Session] = None,
    resolver: Callable[..., Iterable[tuple]] = socket.getaddrinfo,
    timeout: int = REQUEST_TIMEOUT,
) -> PublicMediaValidation:
    client = session or requests.Session()
    owns_session = session is None
    try:
        current_url = public_url
        response = None
        for redirect_count in range(6):
            parsed = urlparse(current_url)
            if (
                parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password
            ):
                raise MediaHostingError(
                    PUBLIC_URL_VALIDATION_FAILED + ": HTTPS URL required"
                )
            _validate_public_host(parsed.hostname, resolver)
            try:
                response = client.get(
                    current_url, timeout=timeout, allow_redirects=False
                )
            except requests.RequestException as error:
                raise MediaHostingError(
                    PUBLIC_URL_VALIDATION_FAILED + ": GET failed"
                ) from error
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("Location")
            if not location or redirect_count == 5:
                raise MediaHostingError(
                    PUBLIC_URL_VALIDATION_FAILED + ": redirect loop"
                )
            current_url = urljoin(current_url, location)
        if response is None:
            raise MediaHostingError(PUBLIC_URL_VALIDATION_FAILED + ": no response")
        if response.status_code != 200:
            raise MediaHostingError(
                PUBLIC_URL_VALIDATION_FAILED + f": HTTP {response.status_code}"
            )
        final_url = current_url
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type not in {"image/png", "image/jpeg"}:
            raise MediaHostingError(
                PUBLIC_URL_VALIDATION_FAILED + ": invalid Content-Type"
            )
        binary = response.content
        remote_hash = hashlib.sha256(binary).hexdigest()
        integrity = MEDIA_HASH_OK
        if expected_sha256 and remote_hash != expected_sha256:
            integrity = MEDIA_HASH_MISMATCH
            raise MediaHostingError(MEDIA_HASH_MISMATCH)
        return PublicMediaValidation(
            public_url=final_url,
            content_type=content_type,
            content_length=len(binary),
            sha256=remote_hash,
            integrity_status=integrity,
        )
    finally:
        if owns_session:
            client.close()


def validate_public_media_url_lightweight(
    public_url: str,
    *,
    session: Optional[requests.Session] = None,
    resolver: Callable[..., Iterable[tuple]] = socket.getaddrinfo,
    timeout: int = REQUEST_TIMEOUT,
) -> PublicMediaValidation:
    """Validate one public asset without downloading its response body."""
    client = session or requests.Session()
    owns_session = session is None
    response = None
    try:
        parsed = urlparse(public_url)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password
        ):
            raise MediaHostingError(
                PUBLIC_URL_VALIDATION_FAILED + ": HTTPS URL required"
            )
        _validate_public_host(parsed.hostname, resolver)
        try:
            response = client.get(
                public_url, timeout=timeout, allow_redirects=True, stream=True
            )
        except requests.RequestException as error:
            raise MediaHostingError(
                PUBLIC_URL_VALIDATION_FAILED + ": GET failed"
            ) from error
        final_url = str(response.url)
        final = urlparse(final_url)
        if final.scheme != "https" or not final.hostname:
            raise MediaHostingError(
                PUBLIC_URL_VALIDATION_FAILED + ": invalid final URL"
            )
        _validate_public_host(final.hostname, resolver)
        if response.status_code != 200:
            raise MediaHostingError(
                PUBLIC_URL_VALIDATION_FAILED + f": HTTP {response.status_code}"
            )
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type != "image/png":
            raise MediaHostingError(
                PUBLIC_URL_VALIDATION_FAILED + ": invalid Content-Type"
            )
        length_value = response.headers.get("Content-Length", "0")
        length = int(length_value) if str(length_value).isdigit() else 0
        return PublicMediaValidation(
            public_url=final_url,
            content_type=content_type,
            content_length=length,
            sha256="",
            integrity_status=PUBLIC_URL_OK,
        )
    finally:
        if response is not None:
            response.close()
        if owns_session:
            client.close()


def load_public_comic_manifest(
    manifest_path: Path = COMIC_PUBLIC_MANIFEST_PATH,
) -> dict[str, PublicComicAsset]:
    """Load the sole comic URL source and cross-check it against stock."""
    try:
        public = json.loads(manifest_path.read_text(encoding="utf-8"))
        stock = json.loads(COMIC_STOCK_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise MediaHostingError(PUBLIC_COMIC_MANIFEST_INVALID) from error
    rows = public.get("items")
    stock_rows = stock.get("items")
    base_url = public.get("base_url")
    if not isinstance(rows, list) or not isinstance(stock_rows, list) or not isinstance(base_url, str):
        raise MediaHostingError(PUBLIC_COMIC_MANIFEST_INVALID)
    stock_by_id = {row.get("comic_id"): row for row in stock_rows if isinstance(row, dict)}
    if len(stock_by_id) != len(stock_rows):
        raise MediaHostingError(PUBLIC_COMIC_MANIFEST_INVALID)
    assets: dict[str, PublicComicAsset] = {}
    public_root = (PROJECT_ROOT / COMIC_GITHUB_PAGES_ASSET_PREFIX).resolve()
    for row in rows:
        if not isinstance(row, dict):
            raise MediaHostingError(PUBLIC_COMIC_MANIFEST_INVALID)
        comic_id = row.get("comic_id")
        filename = row.get("file")
        public_url = row.get("url")
        sha256 = row.get("sha256")
        if (
            not isinstance(comic_id, str)
            or not re.fullmatch(r"comic_\d{3}", comic_id)
            or comic_id in assets
            or filename != f"{comic_id}.png"
            or not isinstance(public_url, str)
            or public_url != f"{base_url.rstrip('/')}/{filename}"
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            raise MediaHostingError(PUBLIC_COMIC_MANIFEST_INVALID)
        parsed = urlparse(public_url)
        source = stock_by_id.get(comic_id)
        local_path = (public_root / filename).resolve()
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or local_path.parent != public_root
            or not local_path.is_file()
            or not isinstance(source, dict)
            or source.get("sha256") != sha256
        ):
            raise MediaHostingError(PUBLIC_COMIC_MANIFEST_INVALID)
        assets[comic_id] = PublicComicAsset(
            comic_id, filename, public_url, sha256, local_path
        )
    if set(assets) != set(stock_by_id) or len(assets) != 50:
        raise MediaHostingError(PUBLIC_COMIC_MANIFEST_INVALID)
    return assets


class GitHubPagesComicMediaHostingProvider(ComicMediaHostingProvider):
    """Resolve already-deployed repository assets; this provider does not push."""

    name = "github_pages"

    def __init__(self, manifest_path: Path = COMIC_PUBLIC_MANIFEST_PATH) -> None:
        self.assets = load_public_comic_manifest(manifest_path)

    def expected_public_url(self, comic_id: str) -> str:
        asset = self.assets.get(comic_id)
        if asset is None:
            raise MediaHostingError(PUBLIC_COMIC_MANIFEST_INVALID)
        return asset.public_url

    def resolve_asset(
        self, comic_id: str, *, validate_remote: bool = False
    ) -> HostedComicAsset:
        asset = self.assets.get(comic_id)
        if asset is None:
            raise MediaHostingError(PUBLIC_COMIC_MANIFEST_INVALID)
        integrity = PUBLIC_URL_OK
        public_url = asset.public_url
        if validate_remote:
            validation = validate_public_media_url_lightweight(public_url)
            public_url = validation.public_url
            integrity = validation.integrity_status
        return HostedComicAsset(
            comic_id=comic_id,
            provider=self.name,
            public_url=public_url,
            sha256=asset.sha256,
            uploaded_at=datetime.now(timezone.utc),
            expires_at=None,
            integrity_status=integrity,
        )

    def publish_asset(
        self, local_path: str, comic_id: str, sha256: str
    ) -> HostedComicAsset:
        path = Path(local_path).resolve()
        try:
            path.relative_to(PROJECT_ROOT)
        except ValueError as error:
            raise MediaHostingError("Comic path is outside the project") from error
        local_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if local_hash != sha256:
            raise MediaHostingError(MEDIA_HASH_MISMATCH)
        asset = self.assets.get(comic_id)
        if asset is None or asset.sha256 != sha256:
            raise MediaHostingError(PUBLIC_COMIC_MANIFEST_INVALID)
        return self.resolve_asset(comic_id, validate_remote=True)


def configured_provider() -> ComicMediaHostingProvider:
    if COMIC_MEDIA_HOSTING_PROVIDER == "disabled":
        return DisabledComicMediaHostingProvider()
    if COMIC_MEDIA_HOSTING_PROVIDER == "github_pages":
        return GitHubPagesComicMediaHostingProvider()
    raise MediaHostingError("Unsupported comic media hosting provider")


def detect_existing_media_hosting() -> MediaHostingDetection:
    pages_candidate = (PROJECT_ROOT / "index.html").is_file()
    if pages_candidate:
        return MediaHostingDetection(
            True,
            "github_pages",
            "Repository has a Pages-compatible index; comic assets require deployment before use",
        )
    return MediaHostingDetection(False, "none", MEDIA_HOSTING_NOT_CONFIGURED)
