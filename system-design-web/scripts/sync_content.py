#!/usr/bin/env python3
"""Generate Hugo Book content from the immutable Chinese translations."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import unicodedata
from pathlib import Path
from urllib.parse import unquote


SITE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SITE_ROOT.parent / "system-design-translated"
BOOK_ROOT = SITE_ROOT / "content" / "system-design"
CHAPTER_RE = re.compile(r"^(0[1-9]|1[0-9]|2[0-8])\.")
H1_RE = re.compile(r"^#\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")
INLINE_DEST_RE = re.compile(
    r"(?P<prefix>!?\[[^\]\n]*\]\()(?P<target><[^>\n]+>|[^)\s]+)(?P<suffix>[^)\n]*\))"
)
REFERENCE_DEST_RE = re.compile(
    r"^(?P<prefix>\s*\[[^\]]+\]:\s*)(?P<target><[^>\n]+>|\S+)(?P<suffix>.*)$",
    re.MULTILINE,
)
BACKTICK_RUN_RE = re.compile(r"`+")


def chapter_slug(directory_name: str) -> str:
    number = int(directory_name[:2])
    label = directory_name.split(".", 1)[1].strip()
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_label).strip("-").lower()
    if not words:
        raise ValueError(f"cannot derive ASCII slug from {directory_name!r}")
    return f"{number:02d}-{words}"


def discover_chapters() -> list[Path]:
    if not SOURCE_ROOT.is_dir():
        raise RuntimeError(f"source directory is missing: {SOURCE_ROOT}")
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
        raise RuntimeError(f"expected chapter range 1..28, found {numbers!r}")
    missing = [str(path / "Readme.md") for path in chapters if not (path / "Readme.md").is_file()]
    if missing:
        raise RuntimeError("missing source Readme.md: " + ", ".join(missing))
    slugs = [chapter_slug(path.name) for path in chapters]
    if len(slugs) != len(set(slugs)):
        raise RuntimeError(f"chapter slugs are not unique: {slugs!r}")
    return chapters


def source_fingerprint() -> str:
    files = sorted(
        (path for path in SOURCE_ROOT.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(SOURCE_ROOT).as_posix().casefold(),
    )
    rows: list[str] = []
    for path in files:
        payload = path.read_bytes()
        rows.append(
            f"{path.relative_to(SOURCE_ROOT).as_posix()}|{len(payload)}|"
            f"{hashlib.sha256(payload).hexdigest().upper()}"
        )
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest().upper()


def first_h1(lines: list[str], source: Path) -> tuple[int, str]:
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
        heading = H1_RE.match(stripped)
        if heading:
            return index, heading.group(1).strip()
    raise RuntimeError(f"source chapter has no H1: {source}")


def split_target(target: str) -> tuple[str, str, str, bool]:
    bracketed = target.startswith("<") and target.endswith(">")
    value = target[1:-1] if bracketed else target
    match = re.match(r"^([^?#]*)(\?[^#]*)?(#.*)?$", value)
    if not match:
        return value, "", "", bracketed
    return match.group(1), match.group(2) or "", match.group(3) or "", bracketed


def rewrite_destination(
    raw_target: str,
    source_chapter: Path,
    slug_by_number: dict[int, str],
    slug_by_path: dict[Path, str],
) -> str:
    path_text, query, fragment, bracketed = split_target(raw_target)
    if (
        not path_text
        or path_text.startswith(("/", "//"))
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path_text)
    ):
        return raw_target

    destination_slug = ""
    decoded_path = unquote(path_text).replace("\\", "/")
    legacy = re.search(r"(?:^|/)chapter(0?[1-9]|1[0-9]|2[0-8])/?$", decoded_path, re.IGNORECASE)
    if legacy:
        destination_slug = slug_by_number[int(legacy.group(1))]
    elif Path(decoded_path).name.casefold() == "readme.md":
        resolved = (source_chapter / decoded_path).resolve()
        destination_slug = slug_by_path.get(resolved.parent, "")

    if not destination_slug:
        return raw_target
    current_slug = slug_by_path[source_chapter.resolve()]
    rewritten = "./" if destination_slug == current_slug else f"../{destination_slug}/"
    rewritten = f"{rewritten}{query}{fragment}"
    return f"<{rewritten}>" if bracketed else rewritten


def rewrite_links(
    text: str,
    source_chapter: Path,
    slug_by_number: dict[int, str],
    slug_by_path: dict[Path, str],
) -> str:
    output: list[str] = []
    plain_text: list[str] = []
    fence_char = ""
    fence_length = 0

    def inline_replacement(match: re.Match[str]) -> str:
        target = rewrite_destination(
            match.group("target"), source_chapter, slug_by_number, slug_by_path
        )
        return f"{match.group('prefix')}{target}{match.group('suffix')}"

    def reference_replacement(match: re.Match[str]) -> str:
        target = rewrite_destination(
            match.group("target"), source_chapter, slug_by_number, slug_by_path
        )
        return f"{match.group('prefix')}{target}{match.group('suffix')}"

    def rewrite_prose(prose: str) -> str:
        rewritten = INLINE_DEST_RE.sub(inline_replacement, prose)
        return REFERENCE_DEST_RE.sub(reference_replacement, rewritten)

    def rewrite_outside_inline_code(prose: str) -> str:
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
            pieces.append(rewrite_prose(prose[outside_start : opener.start()]))
            pieces.append(prose[opener.start() : closer.end()])
            outside_start = closer.end()
            search_start = closer.end()
        pieces.append(rewrite_prose(prose[outside_start:]))
        return "".join(pieces)

    def flush_plain_text() -> None:
        if plain_text:
            output.append(rewrite_outside_inline_code("".join(plain_text)))
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


def yaml_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_chapter(
    source_chapter: Path,
    number: int,
    slug_by_number: dict[int, str],
    slug_by_path: dict[Path, str],
) -> str:
    source_markdown = source_chapter / "Readme.md"
    source_text = source_markdown.read_text(encoding="utf-8")
    lines = source_text.splitlines(keepends=True)
    h1_index, title = first_h1(lines, source_markdown)
    body = "".join(lines[:h1_index] + lines[h1_index + 1 :])
    body = rewrite_links(body, source_chapter, slug_by_number, slug_by_path)
    front_matter = (
        "---\n"
        f"title: {yaml_scalar(title)}\n"
        f"linkTitle: {yaml_scalar(title)}\n"
        f"book_number: {number}\n"
        f"weight: {number * 10}\n"
        "---\n"
    )
    return front_matter + body


def remove_generated_directories(expected_slugs: set[str]) -> int:
    BOOK_ROOT.mkdir(parents=True, exist_ok=True)
    removed = 0
    root_resolved = BOOK_ROOT.resolve()
    for child in sorted(path for path in BOOK_ROOT.iterdir() if path.is_dir()):
        if child.name not in expected_slugs:
            if child.resolve().parent != root_resolved:
                raise RuntimeError(f"refusing to remove directory outside Book root: {child}")
            shutil.rmtree(child)
            removed += 1
    return removed


def copy_resources(source_chapter: Path, target_chapter: Path) -> int:
    count = 0
    for source_path in sorted(source_chapter.rglob("*")):
        relative = source_path.relative_to(source_chapter)
        target_path = target_chapter / relative
        if source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
        elif source_path.suffix.lower() != ".md":
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            count += 1
    return count


def main() -> int:
    before = source_fingerprint()
    chapters = discover_chapters()
    slug_by_number = {
        int(chapter.name[:2]): chapter_slug(chapter.name) for chapter in chapters
    }
    slug_by_path = {
        chapter.resolve(): slug_by_number[int(chapter.name[:2])] for chapter in chapters
    }
    expected_slugs = set(slug_by_number.values())
    stale_removed = remove_generated_directories(expected_slugs)

    resource_count = 0
    for number, source_chapter in enumerate(chapters, start=1):
        slug = slug_by_number[number]
        target_chapter = BOOK_ROOT / slug
        if target_chapter.exists():
            if target_chapter.resolve().parent != BOOK_ROOT.resolve():
                raise RuntimeError(f"refusing to replace directory outside Book root: {target_chapter}")
            shutil.rmtree(target_chapter)
        target_chapter.mkdir(parents=True)
        generated = render_chapter(
            source_chapter, number, slug_by_number, slug_by_path
        )
        (target_chapter / "_index.md").write_text(generated, encoding="utf-8", newline="\n")
        resource_count += copy_resources(source_chapter, target_chapter)

    after = source_fingerprint()
    if before != after:
        raise RuntimeError("system-design-translated changed during synchronization")
    print(
        "SYNC PASSED: "
        f"chapters={len(chapters)} resources={resource_count} "
        f"stale_directories_removed={stale_removed} source_sha256={after}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
