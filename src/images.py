from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote_to_bytes, urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from src.const import BASE_URL


MANIFEST_VERSION = 1
DEFAULT_MAX_IMAGE_BYTES = 25 * 1024 * 1024

MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
}
LAZY_SOURCE_ATTRIBUTES = (
    "data-original",
    "data-lazy-src",
    "data-src",
    "data-url",
    "data-image",
    "data-cfsrc",
)
SOURCESET_ATTRIBUTES = ("data-srcset", "srcset")
BACKGROUND_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


def normalize_image_url(value: str, base_url: str = BASE_URL) -> str:
    value = html.unescape((value or "").strip())
    if not value:
        return ""
    if value.startswith(("data:", "blob:")):
        return value
    absolute = urljoin(base_url, value)
    return urldefrag(absolute)[0]


def _parse_srcset(value: str) -> List[Tuple[str, float]]:
    """Parse the common URL + width/density subset of HTML srcset."""
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("data:"):
        return [(value, 1.0)]

    candidates: List[Tuple[str, float]] = []
    for raw_candidate in value.split(","):
        bits = raw_candidate.strip().split()
        if not bits:
            continue
        score = 1.0
        if len(bits) > 1:
            descriptor = bits[-1].lower()
            try:
                if descriptor.endswith("w"):
                    score = float(descriptor[:-1])
                elif descriptor.endswith("x"):
                    score = float(descriptor[:-1]) * 10000.0
            except ValueError:
                score = 1.0
        candidates.append((bits[0], score))
    return candidates


def _best_srcset(value: str) -> str:
    candidates = _parse_srcset(value)
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[1])[0]


def choose_image_source(image: Tag, base_url: str = BASE_URL) -> str:
    """Choose the highest quality usable source from img/picture lazy attributes."""
    picture = image.parent if isinstance(image.parent, Tag) and image.parent.name == "picture" else None
    if picture is not None:
        for source in picture.find_all("source"):
            for attribute in SOURCESET_ATTRIBUTES:
                candidate = _best_srcset(str(source.get(attribute) or ""))
                if candidate:
                    return normalize_image_url(candidate, base_url)

    for attribute in ("data-srcset",):
        candidate = _best_srcset(str(image.get(attribute) or ""))
        if candidate:
            return normalize_image_url(candidate, base_url)

    for attribute in LAZY_SOURCE_ATTRIBUTES:
        candidate = str(image.get(attribute) or "").strip()
        if candidate:
            return normalize_image_url(candidate, base_url)

    candidate = _best_srcset(str(image.get("srcset") or ""))
    if candidate:
        return normalize_image_url(candidate, base_url)

    return normalize_image_url(str(image.get("src") or ""), base_url)


def normalize_image_tags(soup: BeautifulSoup, base_url: str = BASE_URL) -> None:
    """Normalize lazy/responsive image tags without performing network access."""
    for image in soup.find_all("img"):
        source = choose_image_source(image, base_url)
        if source:
            image["src"] = source


def detect_image_format(
    payload: bytes,
    content_type: str = "",
    source_url: str = "",
) -> Tuple[str, str]:
    """Return (media_type, extension), rejecting HTML/error payloads."""
    if not payload:
        raise ValueError("empty image response")

    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if len(payload) >= 12 and payload[4:8] == b"ftyp":
        brand_block = payload[8:min(len(payload), 64)]
        if b"avif" in brand_block or b"avis" in brand_block:
            return "image/avif", ".avif"

    prefix = payload[:2048].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if prefix.startswith(b"<?xml") or prefix.startswith(b"<svg"):
        if b"<svg" in prefix:
            return "image/svg+xml", ".svg"

    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    raise ValueError(f"response is not a supported image (content-type: {normalized_type or 'unknown'})")


def _decode_data_url(source_url: str) -> Tuple[bytes, str]:
    header, separator, encoded = source_url.partition(",")
    if not separator:
        raise ValueError("invalid data image URL")
    media_type = header[5:].split(";", 1)[0].strip().lower()
    if ";base64" in header.lower():
        try:
            payload = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("invalid base64 data image") from exc
    else:
        payload = unquote_to_bytes(encoded)
    return payload, media_type


@dataclass(frozen=True)
class ImageAsset:
    relative_path: str
    absolute_path: str
    media_type: str
    extension: str
    sha256: str
    size: int


class ImageManager:
    """Download, validate, deduplicate, persist, and inventory novel images."""

    def __init__(
        self,
        session: requests.Session,
        root_dir: str,
        timeout: int = 30,
        manifest_name: str = "images.json",
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    ):
        self.session = session
        self.root_dir = os.path.abspath(root_dir)
        self.asset_dir = os.path.join(self.root_dir, "images")
        self.manifest_path = os.path.join(self.root_dir, manifest_name)
        self.timeout = timeout
        self.max_image_bytes = max_image_bytes
        self.assets: Dict[str, Dict] = {}
        self.source_cache: Dict[str, str] = {}
        self.failures: Dict[str, Dict] = {}
        self.downloaded_this_run = 0
        self.reused_this_run = 0
        os.makedirs(self.asset_dir, exist_ok=True)
        self._load_manifest()

    def _absolute_asset_path(self, relative_path: str) -> Optional[str]:
        normalized = (relative_path or "").replace("\\", "/")
        if not normalized.startswith("images/"):
            return None
        absolute_path = os.path.abspath(os.path.join(self.root_dir, *normalized.split("/")))
        try:
            if os.path.commonpath((self.asset_dir, absolute_path)) != self.asset_dir:
                return None
        except ValueError:
            return None
        return absolute_path

    def _load_manifest(self) -> None:
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
        except (FileNotFoundError, OSError, ValueError):
            return

        for record in data.get("assets") or []:
            if not isinstance(record, dict):
                continue
            digest = str(record.get("sha256") or "")
            relative_path = str(record.get("path") or "")
            absolute_path = self._absolute_asset_path(relative_path)
            if not digest or not absolute_path or not os.path.isfile(absolute_path):
                continue
            if os.path.getsize(absolute_path) != int(record.get("bytes") or -1):
                continue
            self.assets[digest] = record
            for source in record.get("source_urls") or []:
                self.source_cache[str(source)] = digest

    @staticmethod
    def _append_unique(record: Dict, key: str, value: Optional[object]) -> None:
        if value in (None, ""):
            return
        values = record.setdefault(key, [])
        if value not in values:
            values.append(value)

    def _record_usage(
        self,
        record: Dict,
        source_url: str,
        role: str,
        episode_no: Optional[int],
        context: Optional[str],
        alt_text: Optional[str],
    ) -> None:
        self._append_unique(record, "source_urls", source_url)
        self._append_unique(record, "roles", role)
        self._append_unique(record, "episode_numbers", episode_no)
        self._append_unique(record, "contexts", context)
        self._append_unique(record, "alt_texts", (alt_text or "").strip())

    @staticmethod
    def _manifest_source(source_url: str, digest: str) -> str:
        if source_url.startswith("data:"):
            media_type = source_url[5:].split(";", 1)[0].split(",", 1)[0]
            return f"data:{media_type};sha256={digest}"
        return source_url

    def _asset_from_record(self, record: Dict) -> ImageAsset:
        relative_path = str(record["path"])
        absolute_path = self._absolute_asset_path(relative_path)
        if not absolute_path:
            raise ValueError(f"invalid cached image path: {relative_path}")
        return ImageAsset(
            relative_path=relative_path,
            absolute_path=absolute_path,
            media_type=str(record["media_type"]),
            extension=os.path.splitext(relative_path)[1].lower(),
            sha256=str(record["sha256"]),
            size=int(record["bytes"]),
        )

    def _fetch(
        self,
        source_url: str,
        referer: Optional[str],
        request_cookies: Optional[Dict[str, str]] = None,
    ) -> Tuple[bytes, str]:
        if source_url.startswith("data:"):
            return _decode_data_url(source_url)
        if source_url.startswith("blob:"):
            raise ValueError("browser-only blob image URLs cannot be downloaded")
        if urlparse(source_url).scheme not in ("http", "https"):
            raise ValueError("unsupported image URL scheme")

        headers = {
            "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "sec-fetch-dest": "image",
            "sec-fetch-mode": "no-cors",
        }
        if referer:
            headers["referer"] = referer

        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                response = self.session.get(
                    source_url,
                    headers=headers,
                    cookies=request_cookies or None,
                    timeout=self.timeout,
                    stream=True,
                )
                try:
                    if response.status_code == 429:
                        retry_after = response.headers.get("retry-after")
                        try:
                            wait = min(15.0, max(1.0, float(retry_after)))
                        except (TypeError, ValueError):
                            wait = 2.0 * attempt
                        time.sleep(wait)
                        continue
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > self.max_image_bytes:
                        raise ValueError(
                            f"image exceeds {self.max_image_bytes // (1024 * 1024)} MiB limit"
                        )
                    chunks: List[bytes] = []
                    payload_size = 0
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        payload_size += len(chunk)
                        if payload_size > self.max_image_bytes:
                            raise ValueError(
                                f"image exceeds {self.max_image_bytes // (1024 * 1024)} MiB limit"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers.get("content-type", "")
                finally:
                    response.close()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if isinstance(exc, ValueError):
                    break
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code and 400 <= status_code < 500 and status_code != 429:
                    break
                if attempt < 3:
                    time.sleep(float(attempt))
        raise ValueError(str(last_error or "image download failed"))

    def download(
        self,
        source_url: str,
        *,
        role: str = "chapter-image",
        episode_no: Optional[int] = None,
        context: Optional[str] = None,
        alt_text: Optional[str] = None,
        referer: Optional[str] = None,
        base_url: str = BASE_URL,
        request_cookies: Optional[Dict[str, str]] = None,
    ) -> Optional[ImageAsset]:
        source_url = normalize_image_url(source_url, base_url)
        if not source_url:
            return None

        cached_digest = self.source_cache.get(source_url)
        if cached_digest and cached_digest in self.assets:
            record = self.assets[cached_digest]
            self._record_usage(
                record,
                self._manifest_source(source_url, cached_digest),
                role,
                episode_no,
                context,
                alt_text,
            )
            self.reused_this_run += 1
            return self._asset_from_record(record)
        if source_url in self.failures and not request_cookies:
            return None

        try:
            payload, response_type = self._fetch(source_url, referer, request_cookies)
            media_type, extension = detect_image_format(payload, response_type, source_url)
        except Exception as exc:
            display_source = source_url if len(source_url) <= 500 else source_url[:497] + "..."
            self.failures[source_url] = {
                "source_url": display_source,
                "error": str(exc),
                "role": role,
                "episode_no": episode_no,
                "context": context,
            }
            return None

        # A successful authenticated retry supersedes an earlier failure for
        # the same protected URL.
        self.failures.pop(source_url, None)

        digest = hashlib.sha256(payload).hexdigest()
        record = self.assets.get(digest)
        if record is None:
            filename = f"{digest[:24]}{extension}"
            relative_path = f"images/{filename}"
            absolute_path = os.path.join(self.asset_dir, filename)
            existing_digest = ""
            if os.path.isfile(absolute_path):
                with open(absolute_path, "rb") as handle:
                    existing_digest = hashlib.sha256(handle.read()).hexdigest()
            if existing_digest != digest:
                temporary_path = absolute_path + ".part"
                with open(temporary_path, "wb") as handle:
                    handle.write(payload)
                os.replace(temporary_path, absolute_path)
            record = {
                "path": relative_path,
                "sha256": digest,
                "media_type": media_type,
                "bytes": len(payload),
                "source_urls": [],
                "roles": [],
                "episode_numbers": [],
                "contexts": [],
                "alt_texts": [],
            }
            self.assets[digest] = record
            self.downloaded_this_run += 1
        else:
            self.reused_this_run += 1

        self.source_cache[source_url] = digest
        recorded_source = self._manifest_source(source_url, digest)
        self._record_usage(record, recorded_source, role, episode_no, context, alt_text)
        return self._asset_from_record(record)

    @staticmethod
    def _strip_remote_candidates(image: Tag) -> None:
        for attribute in (*LAZY_SOURCE_ATTRIBUTES, *SOURCESET_ATTRIBUTES):
            image.attrs.pop(attribute, None)
        picture = image.parent if isinstance(image.parent, Tag) and image.parent.name == "picture" else None
        if picture is not None:
            for source in picture.find_all("source"):
                source.decompose()

    def localize_html(
        self,
        html_text: str,
        *,
        episode_no: Optional[int] = None,
        context: Optional[str] = None,
        referer: Optional[str] = None,
        base_url: str = BASE_URL,
        request_cookies: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, List[ImageAsset]]:
        soup = BeautifulSoup(html_text or "", "html.parser")
        used: Dict[str, ImageAsset] = {}

        for image in soup.find_all("img"):
            source_url = choose_image_source(image, base_url)
            alt_text = str(image.get("alt") or "").strip()
            asset = self.download(
                source_url,
                role="chapter-image",
                episode_no=episode_no,
                context=context,
                alt_text=alt_text,
                referer=referer,
                base_url=base_url,
                request_cookies=request_cookies,
            )
            if asset:
                image["src"] = asset.relative_path
                used[asset.sha256] = asset
            else:
                label = alt_text or "Image unavailable"
                placeholder = soup.new_tag("span")
                placeholder["class"] = "missing-image"
                placeholder.string = f"[{label}]"
                image.replace_with(placeholder)
                continue
            self._strip_remote_candidates(image)

        for element in soup.find_all(style=True):
            style = str(element.get("style") or "")

            def replace_background(match: re.Match) -> str:
                source_url = normalize_image_url(match.group(2), base_url)
                asset = self.download(
                    source_url,
                    role="chapter-background",
                    episode_no=episode_no,
                    context=context,
                    referer=referer,
                    base_url=base_url,
                    request_cookies=request_cookies,
                )
                if not asset:
                    return "none"
                used[asset.sha256] = asset
                return f"url('{asset.relative_path}')"

            element["style"] = BACKGROUND_URL_RE.sub(replace_background, style)

        for style_element in soup.find_all("style"):
            css_text = style_element.string
            if not css_text:
                continue

            def replace_stylesheet_url(match: re.Match) -> str:
                source_url = normalize_image_url(match.group(2), base_url)
                asset = self.download(
                    source_url,
                    role="chapter-stylesheet-image",
                    episode_no=episode_no,
                    context=context,
                    referer=referer,
                    base_url=base_url,
                    request_cookies=request_cookies,
                )
                if not asset:
                    return "none"
                used[asset.sha256] = asset
                return f"url('{asset.relative_path}')"

            style_element.string.replace_with(BACKGROUND_URL_RE.sub(replace_stylesheet_url, str(css_text)))

        return str(soup), list(used.values())

    def save_manifest(self) -> None:
        records = sorted(self.assets.values(), key=lambda item: str(item.get("path") or ""))
        failures = sorted(self.failures.values(), key=lambda item: str(item.get("source_url") or ""))
        temporary_path = self.manifest_path + ".part"
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": MANIFEST_VERSION,
                    "assets": records,
                    "failures": failures,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(temporary_path, self.manifest_path)

    def summary(self) -> Dict:
        cover_path = ""
        for record in self.assets.values():
            if "cover" in (record.get("roles") or []):
                cover_path = str(record.get("path") or "")
                break
        return {
            "image_count": len(self.assets),
            "image_bytes": sum(int(record.get("bytes") or 0) for record in self.assets.values()),
            "image_failures": len(self.failures),
            "images_downloaded_this_run": self.downloaded_this_run,
            "images_reused_this_run": self.reused_this_run,
            "cover_image": cover_path or None,
            "images_manifest": os.path.basename(self.manifest_path),
        }

    def failure_messages(self) -> Iterable[str]:
        for failure in self.failures.values():
            yield f"{failure.get('source_url')}: {failure.get('error')}"
