#!/usr/bin/env python3
"""Reconstruct an already signed APK from an immutable base and public byte literals.

No signing operation or key access is involved. ZIP payloads are copied as raw
compressed bytes; local headers, signing blocks and the directory are literals.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import zipfile

MAX_APK = 512 * 1024 * 1024
MAX_MANIFEST = 4 * 1024 * 1024
MAX_SEGMENTS = 20000
SHA = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.apk$")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()


def extent(path, offset, length):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        stream.seek(offset)
        while length:
            block = stream.read(min(length, 1024 * 1024))
            if not block: raise ValueError("Truncated APK extent")
            h.update(block); length -= len(block)
    return h.hexdigest()


def compressed_extents(path):
    result = {}
    with zipfile.ZipFile(path) as archive, Path(path).open("rb") as stream:
        infos = sorted(archive.infolist(), key=lambda value: value.header_offset)
        if len({x.filename for x in infos}) != len(infos): raise ValueError("Duplicate APK entries")
        end = 0
        for info in infos:
            stream.seek(info.header_offset)
            local = stream.read(30)
            if len(local) != 30 or local[:4] != b"PK\x03\x04": raise ValueError("Invalid APK local header")
            name, extra = struct.unpack_from("<HH", local, 26)
            start = info.header_offset + 30 + name + extra
            if info.header_offset < end or start + info.compress_size > archive.start_dir:
                raise ValueError("Overlapping APK entries")
            end = start + info.compress_size
            result[info.filename] = (start, info.compress_size)
    return result


def checked_int(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper: raise ValueError("Invalid extent integer")
    return value


def manifest(archive):
    infos = archive.infolist()
    names = [x.filename for x in infos]
    if len(names) != len(set(names)) or "manifest.json" not in names: raise ValueError("Invalid kit entries")
    meta = archive.getinfo("manifest.json")
    if meta.file_size > MAX_MANIFEST: raise ValueError("Manifest too large")
    value = json.loads(archive.read(meta))
    if set(value) != {"schema", "base", "target", "segments"} or value["schema"] != 1:
        raise ValueError("Unknown reconstruction schema")
    for key in ("base", "target"):
        item = value[key]
        if set(item) != {"name", "bytes", "sha256"}: raise ValueError("Invalid artifact metadata")
        if not NAME.fullmatch(item["name"]) or not SHA.fullmatch(item["sha256"]): raise ValueError("Invalid artifact name/hash")
        checked_int(item["bytes"], 1, MAX_APK)
    segments = value["segments"]
    if not isinstance(segments, list) or not 0 < len(segments) <= MAX_SEGMENTS: raise ValueError("Invalid segment count")
    expected = {"manifest.json"}; total = 0
    for index, part in enumerate(segments):
        if not isinstance(part, dict) or not SHA.fullmatch(part.get("sha256", "")): raise ValueError("Invalid segment hash")
        length = checked_int(part.get("length"), 1, value["target"]["bytes"])
        total += length
        if part.get("kind") == "base":
            if set(part) != {"kind", "offset", "length", "sha256"}: raise ValueError("Invalid copy segment")
            checked_int(part["offset"], 0, value["base"]["bytes"] - length)
        elif part.get("kind") == "literal":
            if set(part) != {"kind", "entry", "length", "sha256"}: raise ValueError("Invalid literal segment")
            name = "parts/%06d.bin" % index
            if part["entry"] != name: raise ValueError("Noncanonical literal path")
            expected.add(name)
            if archive.getinfo(name).file_size != length: raise ValueError("Literal size mismatch")
        else: raise ValueError("Unknown segment kind")
    if total != value["target"]["bytes"] or set(names) != expected: raise ValueError("Segment coverage mismatch")
    if sum(x.file_size for x in infos) > value["target"]["bytes"] + MAX_MANIFEST:
        raise ValueError("Kit expansion exceeds target")
    return value


def rebuild(base, kit, output, expected_kit_sha=None):
    base, kit, output = Path(base), Path(kit), Path(output)
    if output.exists(): raise FileExistsError("Refusing to overwrite output")
    if expected_kit_sha and digest(kit) != expected_kit_sha: raise ValueError("Kit SHA-256 mismatch")
    with zipfile.ZipFile(kit) as archive:
        value = manifest(archive)
        if base.stat().st_size != value["base"]["bytes"] or digest(base) != value["base"]["sha256"]:
            raise ValueError("Base APK SHA-256/size mismatch")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".reconstruct-", dir=output.parent, delete=False) as target, base.open("rb") as original:
                temporary = Path(target.name); full = hashlib.sha256(); count = 0
                for part in value["segments"]:
                    h = hashlib.sha256(); remaining = part["length"]
                    if part["kind"] == "base":
                        original.seek(part["offset"]); stream = original
                    else: stream = archive.open(part["entry"])
                    try:
                        while remaining:
                            block = stream.read(min(remaining, 1024 * 1024))
                            if not block: raise ValueError("Truncated reconstruction segment")
                            target.write(block); h.update(block); full.update(block)
                            remaining -= len(block); count += len(block)
                    finally:
                        if part["kind"] == "literal": stream.close()
                    if h.hexdigest() != part["sha256"]: raise ValueError("Segment SHA-256 mismatch")
                target.flush(); os.fsync(target.fileno())
                if count != value["target"]["bytes"] or full.hexdigest() != value["target"]["sha256"]:
                    raise ValueError("Reconstructed APK SHA-256 mismatch")
            # Same-directory hard-link creation is atomic and never replaces a file
            # that appeared after the initial existence check.
            os.link(temporary, output); temporary.unlink(); temporary = None
        finally:
            if temporary is not None: temporary.unlink(missing_ok=True)
    return value


def make(base, target, output):
    base, target, output = Path(base), Path(target), Path(output)
    if output.exists(): raise FileExistsError("Refusing to overwrite kit")
    for path in (base, target):
        if not NAME.fullmatch(path.name) or not 0 < path.stat().st_size <= MAX_APK: raise ValueError("Invalid APK input")
    originals, current = compressed_extents(base), compressed_extents(target)
    segments = []; copied = 0; literal = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".pending")
    if temporary.exists(): raise FileExistsError("Stale pending kit")
    def add_literal(archive, start, length):
        nonlocal literal
        if not length: return
        name = "parts/%06d.bin" % len(segments)
        info = zipfile.ZipInfo(name, (1981, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_DEFLATED
        h = hashlib.sha256()
        with target.open("rb") as source, archive.open(info, "w") as dest:
            source.seek(start); remaining = length
            while remaining:
                block = source.read(min(remaining, 1024 * 1024))
                if not block: raise ValueError("Truncated target")
                dest.write(block); h.update(block); remaining -= len(block)
        segments.append(dict(kind="literal", entry=name, length=length, sha256=h.hexdigest())); literal += length
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            cursor = 0
            for name, (start, length) in sorted(current.items(), key=lambda item: item[1][0]):
                add_literal(archive, cursor, start - cursor)
                old = originals.get(name)
                checksum = extent(target, start, length)
                if length and old and old[1] == length and extent(base, *old) == checksum:
                    segments.append(dict(kind="base", offset=old[0], length=length, sha256=checksum)); copied += length
                else: add_literal(archive, start, length)
                cursor = start + length
            add_literal(archive, cursor, target.stat().st_size - cursor)
            value = dict(schema=1, base=dict(name=base.name, bytes=base.stat().st_size, sha256=digest(base)),
                         target=dict(name=target.name, bytes=target.stat().st_size, sha256=digest(target)), segments=segments)
            info = zipfile.ZipInfo("manifest.json", (1981, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
        # Byte-identical local reconstruction is mandatory before exposing a kit.
        with tempfile.TemporaryDirectory(prefix="apk-roundtrip-", dir=output.parent) as directory:
            rebuilt = Path(directory) / target.name
            rebuild(base, temporary, rebuilt)
            if digest(rebuilt) != digest(target): raise ValueError("Local reconstruction differs")
        os.link(temporary, output); temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)
    return dict(kit=str(output), kitBytes=output.stat().st_size, kitSha256=digest(output),
                copiedBytes=copied, literalBytes=literal, target=value["target"], localRoundtripExact=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("make"); create.add_argument("--base", required=True, type=Path)
    create.add_argument("--target", required=True, type=Path); create.add_argument("--output", required=True, type=Path)
    restore = sub.add_parser("rebuild"); restore.add_argument("--base", required=True, type=Path)
    restore.add_argument("--kit", required=True, type=Path); restore.add_argument("--output", required=True, type=Path)
    restore.add_argument("--kit-sha256")
    inspect = sub.add_parser("inspect"); inspect.add_argument("--kit", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "make": result = make(args.base, args.target, args.output)
    elif args.command == "rebuild":
        result = rebuild(args.base, args.kit, args.output, args.kit_sha256)
        result = dict(output=str(args.output), sha256=result["target"]["sha256"], bytes=result["target"]["bytes"])
    else:
        with zipfile.ZipFile(args.kit) as archive: result = manifest(archive)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
