"""Comic media-hosting providers and strict public URL validation."""

import hashlib
import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import quote, urljoin, urlparse

import requests

from app.config import (
    COMIC_GITHUB_PAGES_BASE_URL,
    COMIC_GITHUB_PAGES_ASSET_PREFIX,
    COMIC_MEDIA_HOSTING_PROVIDER,
    PROJECT_ROOT,
    REQUEST_TIMEOUT,
)


MEDIA_HASH_OK = "MEDIA_HASH_OK"
MEDIA_HASH_MISMATCH = "MEDIA_HASH_MISMATCH"
MEDIA_HOSTING_NOT_CONFIGURED = "MEDIA_HOSTING_NOT_CONFIGURED"
PUBLIC_URL_VALIDATION_FAILED = "PUBLIC_URL_VALIDATION_FAILED"


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


class GitHubPagesComicMediaHostingProvider(ComicMediaHostingProvider):
    """Resolve already-deployed repository assets; this provider does not push."""

    name = "github_pages"

    def __init__(self, base_url: str = COMIC_GITHUB_PAGES_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    def expected_public_url(self, comic_id: str) -> str:
        if not re.fullmatch(r"comic_\d{3}", comic_id):
            raise MediaHostingError("Invalid comic_id for GitHub Pages")
        prefix = COMIC_GITHUB_PAGES_ASSET_PREFIX.strip("/")
        return f"{self.base_url}/{prefix}/{quote(comic_id)}.png"

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
        public_url = self.expected_public_url(comic_id)
        validation = validate_public_media_url(
            public_url, expected_sha256=sha256
        )
        return HostedComicAsset(
            comic_id=comic_id,
            provider=self.name,
            public_url=validation.public_url,
            sha256=sha256,
            uploaded_at=datetime.now(timezone.utc),
            expires_at=None,
            integrity_status=validation.integrity_status,
        )


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
