#!/usr/bin/env python3
"""Validate the generated Hugo book against its translated source."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin


SITE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SITE_ROOT.parent / "system-design-translated"
BOOK_ROOT = SITE_ROOT / "content" / "system-design"
HOME_DATA = SITE_ROOT / "data" / "home.yaml"
PUBLIC_ROOT = SITE_ROOT / "public"
EXPECTED_SOURCE_FILES = 420
EXPECTED_SOURCE_TREE_SHA256 = (
    "3F52A315EC9AE5D81E5B51235632AC27C0E97B5CDBA5BA5D2094132E52D9C3A5"
)
CHAPTER_RE = re.compile(r"^(0[1-9]|1[0-9]|2[0-8])\.")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")
BACKTICK_RUN_RE = re.compile(r"`+")
INLINE_DEST_RE = re.compile(
    r"(?P<prefix>(?<!!)\[[^\]\n]*\]\()(?P<target><[^>\n]+>|[^)\s]+)(?P<suffix>[^)\n]*\))"
)
REFERENCE_DEST_RE = re.compile(
    r"^(?P<prefix>\s*\[[^\]]+\]:\s*)(?P<target><[^>\n]+>|\S+)(?P<suffix>.*)$",
    re.MULTILINE,
)
INLINE_LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)")
REFERENCE_LINK_RE = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\(([^)\n]+)\)")
HTML_IMAGE_RE = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*(['\"])(.*?)\1", re.IGNORECASE
)
EXTERNAL_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


class HomePageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.canonical = ""
        self.primary_actions: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "link" and "canonical" in (attributes.get("rel") or "").split():
            self.canonical = attributes.get("href") or ""
        if tag == "a" and "td-landing-button--primary" in (
            attributes.get("class") or ""
        ).split():
            self.primary_actions.append(attributes.get("href") or "")


def source_tree_digest(root: Path) -> tuple[int, str]:
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    rows: list[str] = []
    for path in files:
        payload = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256(payload).hexdigest().upper()
        rows.append(f"{relative}|{len(payload)}|{digest}")
    aggregate = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest().upper()
    return len(files), aggregate


def chapter_slug(directory_name: str) -> str:
    number = int(directory_name[:2])
    label = directory_name.split(".", 1)[1].strip()
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_label).strip("-").lower()
    if not words:
        raise ValueError(f"cannot derive ASCII slug from {directory_name!r}")
    return f"{number:02d}-{words}"


def discover_source_chapters(errors: list[str]) -> list[Path]:
    if not SOURCE_ROOT.is_dir():
        errors.append(f"source directory is missing: {SOURCE_ROOT}")
        return []
    chapters = sorted(
        (
            path
            for path in SOURCE_ROOT.iterdir()
            if path.is_dir() and CHAPTER_RE.match(path.name)
        ),
        key=lambda path: int(path.name[:2]),
    )
    numbers = [int(path.name[:2]) for path in chapters]
    if len(chapters) != 28 or numbers != list(range(1, 29)):
        errors.append(f"source chapter range is {numbers!r}, expected 1..28")
    for chapter in chapters:
        if not (chapter / "Readme.md").is_file():
            errors.append(f"source Readme.md is missing: {chapter / 'Readme.md'}")
    return chapters


def split_front_matter(text: str, path: Path, errors: list[str]) -> tuple[dict[str, str], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        errors.append(f"YAML front matter is missing: {path}")
        return {}, text
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        errors.append(f"YAML front matter is unterminated: {path}")
        return {}, text
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$", line)
        if match:
            fields[match.group(1)] = match.group(2)
    return fields, "".join(lines[end + 1 :])


def decode_yaml_scalar(value: str) -> str:
    if value.startswith(('"', "'")):
        if value.startswith('"'):
            return str(json.loads(value))
        return value[1:-1].replace("''", "'")
    return value


def plain_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        match = FENCE_RE.match(stripped)
        if not fence_char and match:
            if current:
                chunks.append("".join(current))
                current = []
            fence_char = match.group(1)[0]
            fence_length = len(match.group(1))
            continue
        if fence_char:
            if re.match(rf"^ {{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*$", stripped):
                fence_char = ""
                fence_length = 0
            continue
        current.append(line)
    if current:
        chunks.append("".join(current))
    return chunks


def inline_code_spans(text: str) -> list[str]:
    spans: list[str] = []
    for chunk in plain_chunks(text):
        search_start = 0
        while opener := BACKTICK_RUN_RE.search(chunk, search_start):
            closer = next(
                (
                    candidate
                    for candidate in BACKTICK_RUN_RE.finditer(chunk, opener.end())
                    if len(candidate.group()) == len(opener.group())
                ),
                None,
            )
            if closer is None:
                search_start = opener.end()
                continue
            spans.append(chunk[opener.start() : closer.end()])
            search_start = closer.end()
    return spans


def outside_protected(text: str) -> str:
    output: list[str] = []
    for chunk in plain_chunks(text):
        outside_start = 0
        search_start = 0
        while opener := BACKTICK_RUN_RE.search(chunk, search_start):
            closer = next(
                (
                    candidate
                    for candidate in BACKTICK_RUN_RE.finditer(chunk, opener.end())
                    if len(candidate.group()) == len(opener.group())
                ),
                None,
            )
            if closer is None:
                search_start = opener.end()
                continue
            output.append(chunk[outside_start : opener.start()])
            protected = chunk[opener.start() : closer.end()]
            output.append(
                "".join("\n" if character == "\n" else " " for character in protected)
            )
            outside_start = closer.end()
            search_start = closer.end()
        output.append(chunk[outside_start:])
    return "".join(output)


def transform_outside_protected(text: str, transform: Callable[[str], str]) -> str:
    output: list[str] = []
    plain_text: list[str] = []
    fence_char = ""
    fence_length = 0

    def transform_plain(prose: str) -> str:
        pieces: list[str] = []
        outside_start = 0
        search_start = 0
        while opener := BACKTICK_RUN_RE.search(prose, search_start):
            closer = next(
                (
                    candidate
                    for candidate in BACKTICK_RUN_RE.finditer(prose, opener.end())
                    if len(candidate.group()) == len(opener.group())
                ),
                None,
            )
            if closer is None:
                search_start = opener.end()
                continue
            pieces.append(transform(prose[outside_start : opener.start()]))
            pieces.append(prose[opener.start() : closer.end()])
            outside_start = closer.end()
            search_start = closer.end()
        pieces.append(transform(prose[outside_start:]))
        return "".join(pieces)

    def flush_plain_text() -> None:
        if plain_text:
            output.append(transform_plain("".join(plain_text)))
            plain_text.clear()

    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        match = FENCE_RE.match(stripped)
        if not fence_char and match:
            flush_plain_text()
            fence_char = match.group(1)[0]
            fence_length = len(match.group(1))
            output.append(line)
            continue
        if fence_char:
            output.append(line)
            if re.match(rf"^ {{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*$", stripped):
                fence_char = ""
                fence_length = 0
            continue
        plain_text.append(line)
    flush_plain_text()
    return "".join(output)


def fenced_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        match = FENCE_RE.match(stripped)
        if not fence_char and match:
            fence_char = match.group(1)[0]
            fence_length = len(match.group(1))
            current = [line]
            continue
        if fence_char:
            current.append(line)
            if re.match(rf"^ {{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*$", stripped):
                blocks.append("".join(current))
                current = []
                fence_char = ""
                fence_length = 0
    if current:
        blocks.append("".join(current))
    return blocks


def link_destination(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    return value.split(maxsplit=1)[0]


def source_body_and_title(text: str, path: Path, errors: list[str]) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    fence_char = ""
    fence_length = 0
    for index, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        match = FENCE_RE.match(stripped)
        if not fence_char and match:
            fence_char = match.group(1)[0]
            fence_length = len(match.group(1))
            continue
        if fence_char:
            if re.match(rf"^ {{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*$", stripped):
                fence_char = ""
                fence_length = 0
            continue
        heading = re.match(r"^#\s+(.+?)\s*$", stripped)
        if heading:
            return "".join(lines[:index] + lines[index + 1 :]), heading.group(1).strip()
    errors.append(f"source chapter has no H1: {path}")
    return text, ""


def split_destination(target: str) -> tuple[str, str, str, bool]:
    bracketed = target.startswith("<") and target.endswith(">")
    value = target[1:-1] if bracketed else target
    match = re.match(r"^([^?#]*)(\?[^#]*)?(#.*)?$", value)
    if not match:
        return value, "", "", bracketed
    return match.group(1), match.group(2) or "", match.group(3) or "", bracketed


def canonical_chapter_destination(
    raw_target: str,
    chapter_directory: Path,
    number_by_directory: dict[Path, int],
    source_side: bool,
) -> str:
    path_text, query, fragment, bracketed = split_destination(raw_target)
    if (
        not path_text
        or path_text.startswith(("/", "//"))
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path_text)
    ):
        return raw_target

    number: int | None = None
    decoded_path = unquote(path_text).replace("\\", "/")
    if source_side:
        legacy = re.search(
            r"(?:^|/)chapter(0?[1-9]|1[0-9]|2[0-8])/?$",
            decoded_path,
            re.IGNORECASE,
        )
        if legacy:
            number = int(legacy.group(1))
        elif Path(decoded_path).name.casefold() == "readme.md":
            number = number_by_directory.get((chapter_directory / decoded_path).resolve().parent)
    else:
        resolved = (chapter_directory / decoded_path).resolve()
        if resolved.is_file() and resolved.name == "_index.md":
            resolved = resolved.parent
        number = number_by_directory.get(resolved)

    if number is None:
        return raw_target
    canonical = f"@@CHAPTER:{number:02d}@@{query}{fragment}"
    return f"<{canonical}>" if bracketed else canonical


def canonicalize_chapter_links(
    text: str,
    chapter_directory: Path,
    number_by_directory: dict[Path, int],
    source_side: bool,
) -> str:
    def rewrite_prose(prose: str) -> str:
        def replace(match: re.Match[str]) -> str:
            target = canonical_chapter_destination(
                match.group("target"), chapter_directory, number_by_directory, source_side
            )
            return f"{match.group('prefix')}{target}{match.group('suffix')}"

        rewritten = INLINE_DEST_RE.sub(replace, prose)
        return REFERENCE_DEST_RE.sub(replace, rewritten)

    return transform_outside_protected(text, rewrite_prose)


def is_external_or_anchor(target: str) -> bool:
    return bool(
        target.startswith(("#", "//"))
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target)
    )


def local_target_candidates(markdown_path: Path, path_part: str) -> list[Path]:
    if path_part.startswith("/"):
        relative = path_part.lstrip("/")
        if not relative:
            return [SITE_ROOT / "content"]
        return [SITE_ROOT / "content" / relative, SITE_ROOT / "static" / relative]
    return [markdown_path.parent / path_part]


def valid_local_target(markdown_path: Path, path_part: str, allow_directory: bool) -> bool:
    site_root = SITE_ROOT.resolve()
    for candidate in local_target_candidates(markdown_path, path_part):
        resolved = candidate.resolve()
        try:
            resolved.relative_to(site_root)
        except ValueError:
            continue
        if resolved.is_file():
            return True
        if allow_directory and resolved.is_dir() and (resolved / "_index.md").is_file():
            return True
    return False


def check_local_link(markdown_path: Path, target: str, errors: list[str]) -> None:
    if is_external_or_anchor(target):
        return
    path_part = unquote(target.split("#", 1)[0].split("?", 1)[0])
    if not path_part:
        return
    if not valid_local_target(markdown_path, path_part, allow_directory=True):
        errors.append(f"internal Markdown link is broken: {markdown_path} -> {target}")


def relative_resource_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() != ".md":
            manifest[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def check_built_home(errors: list[str]) -> None:
    home_page = PUBLIC_ROOT / "index.html"
    if not home_page.is_file():
        errors.append(f"built home page is missing: {home_page}")
        return
    parser = HomePageParser()
    parser.feed(home_page.read_text(encoding="utf-8"))
    if not parser.canonical:
        errors.append(f"built home page has no canonical URL: {home_page}")
        return
    if len(parser.primary_actions) != 1:
        errors.append(
            "built home page must have exactly one primary action: "
            f"found {len(parser.primary_actions)} in {home_page}"
        )
        return
    expected = urljoin(parser.canonical, "system-design/")
    actual = urljoin(parser.canonical, parser.primary_actions[0])
    if actual != expected:
        errors.append(
            "built home page primary action does not target the deployed book: "
            f"{actual}, expected {expected}"
        )


def main(check_public: bool = False) -> int:
    errors: list[str] = []
    chapters = discover_source_chapters(errors)
    source_count, source_digest = source_tree_digest(SOURCE_ROOT) if SOURCE_ROOT.is_dir() else (0, "")
    if source_count != EXPECTED_SOURCE_FILES or source_digest != EXPECTED_SOURCE_TREE_SHA256:
        errors.append(
            "system-design-translated changed: "
            f"files={source_count}, sha256={source_digest}"
        )

    if not HOME_DATA.is_file():
        errors.append(f"OINK home data is missing: {HOME_DATA}")
    else:
        home_data = HOME_DATA.read_text(encoding="utf-8")
        required_home_patterns = {
            "sections list": r"(?m)^sections:\s*$",
            "hero section": r"(?m)^\s*-\s*hero\s*$",
            "hero data": r"(?m)^hero:\s*$",
        }
        for label, pattern in required_home_patterns.items():
            if not re.search(pattern, home_data):
                errors.append(f"OINK home data has no {label}: {HOME_DATA}")

    if not BOOK_ROOT.is_dir():
        errors.append(f"target book root is missing: {BOOK_ROOT}")
        print("CHECK FAILED")
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    expected = {chapter_slug(path.name): path for path in chapters}
    source_number_by_directory = {
        chapter.resolve(): number for number, chapter in enumerate(chapters, start=1)
    }
    target_number_by_directory = {
        (BOOK_ROOT / slug).resolve(): number
        for number, slug in enumerate(expected, start=1)
    }
    target_dirs = sorted(path for path in BOOK_ROOT.iterdir() if path.is_dir())
    actual_slugs = {path.name for path in target_dirs}
    if len(target_dirs) != 28:
        errors.append(f"target chapter count is {len(target_dirs)}, expected 28")
    if actual_slugs != set(expected):
        errors.append(
            "target/source mapping mismatch: "
            f"missing={sorted(set(expected) - actual_slugs)}, "
            f"extra={sorted(actual_slugs - set(expected))}"
        )

    section_index = BOOK_ROOT / "_index.md"
    if not section_index.is_file():
        errors.append(f"Book section index is missing: {section_index}")

    copied_resources = 0
    for number, (slug, source_chapter) in enumerate(expected.items(), start=1):
        target_chapter = BOOK_ROOT / slug
        target_markdown = target_chapter / "_index.md"
        source_markdown = source_chapter / "Readme.md"
        if not target_markdown.is_file():
            errors.append(f"target chapter is missing: {target_markdown}")
            continue
        target_text = target_markdown.read_text(encoding="utf-8")
        if not target_text.strip():
            errors.append(f"target chapter is empty: {target_markdown}")
            continue
        fields, target_body = split_front_matter(target_text, target_markdown, errors)
        source_text = source_markdown.read_text(encoding="utf-8")
        source_body, expected_title = source_body_and_title(source_text, source_markdown, errors)
        for field in ("title", "book_number", "weight"):
            if field not in fields:
                errors.append(f"front matter field {field!r} is missing: {target_markdown}")
        if "title" in fields and decode_yaml_scalar(fields["title"]) != expected_title:
            errors.append(f"front matter title does not match source H1: {target_markdown}")
        if "linkTitle" not in fields or decode_yaml_scalar(fields.get("linkTitle", "")) != expected_title:
            errors.append(f"front matter linkTitle does not match source H1: {target_markdown}")
        try:
            if int(fields.get("book_number", "-1")) != number:
                errors.append(f"book_number is not {number}: {target_markdown}")
            if int(fields.get("weight", "-1")) != number * 10:
                errors.append(f"weight is not {number * 10}: {target_markdown}")
        except ValueError:
            errors.append(f"book_number or weight is not an integer: {target_markdown}")

        if fenced_blocks(source_body) != fenced_blocks(target_body):
            errors.append(f"fenced code blocks changed: {target_markdown}")
        if inline_code_spans(source_body) != inline_code_spans(target_body):
            errors.append(f"inline code spans changed: {target_markdown}")
        if EXTERNAL_URL_RE.findall(source_body) != EXTERNAL_URL_RE.findall(target_body):
            errors.append(f"external URLs changed: {target_markdown}")
        source_signature = canonicalize_chapter_links(
            source_body,
            source_chapter,
            source_number_by_directory,
            source_side=True,
        )
        target_signature = canonicalize_chapter_links(
            target_body,
            target_chapter,
            target_number_by_directory,
            source_side=False,
        )
        if source_signature != target_signature:
            errors.append(
                f"generated body changed beyond H1 removal and chapter-link rewriting: {target_markdown}"
            )

        visible_text = outside_protected(target_body)
        link_targets = [link_destination(raw) for raw in INLINE_LINK_RE.findall(visible_text)]
        link_targets.extend(REFERENCE_LINK_RE.findall(visible_text))
        for target in link_targets:
            if re.search(r"Readme\.md(?:$|[?#])", target, re.IGNORECASE):
                errors.append(f"generated link still points to Readme.md: {target_markdown} -> {target}")
            check_local_link(target_markdown, target, errors)

        image_targets = [link_destination(raw) for raw in MARKDOWN_IMAGE_RE.findall(visible_text)]
        image_targets.extend(match[1] for match in HTML_IMAGE_RE.findall(visible_text))
        for target in image_targets:
            if is_external_or_anchor(target):
                continue
            image_path = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not valid_local_target(target_markdown, image_path, allow_directory=False):
                errors.append(f"image target is missing: {target_markdown} -> {target}")

        source_resources = relative_resource_manifest(source_chapter)
        target_resources = relative_resource_manifest(target_chapter)
        copied_resources += len(target_resources)
        if source_resources != target_resources:
            errors.append(f"static resource manifest changed: {target_chapter}")

    if check_public:
        check_built_home(errors)

    if errors:
        print("CHECK FAILED")
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        "CHECK PASSED: "
        f"source_chapters={len(chapters)} target_chapters={len(target_dirs)} "
        f"resources={copied_resources} source_sha256={source_digest}"
    )
    return 0


if __name__ == "__main__":
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument(
        "--public", action="store_true", help="also validate the rendered public site"
    )
    arguments = argument_parser.parse_args()
    raise SystemExit(main(check_public=arguments.public))
