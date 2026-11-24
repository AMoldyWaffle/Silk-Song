#!/usr/bin/env python3
"""Create experimental lower-initial-memory copies of a Unity WebGL WASM build.

The original Build/w-pt.wasm.unityweb is never modified. Each candidate keeps
the module's declared maximum memory and lowers only its initial minimum.
These are experiments, not guaranteed working Unity builds.
"""
import gzip
import json
from pathlib import Path

SOURCE = Path("Build/w-pt.wasm.unityweb")
OUT = Path("wasm-memory-experiments")
CANDIDATE_MIB = (512, 768, 1024, 1536)
PAGE_SIZE = 65536

def read_u32(data, pos):
    value = shift = 0
    while True:
        if pos >= len(data) or shift > 35:
            raise ValueError("invalid or truncated ULEB128")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7

def write_u32(value):
    if value < 0:
        raise ValueError("ULEB128 value cannot be negative")
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7f) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)

def parse_sections(wasm):
    if wasm[:8] != b"\0asm\x01\0\0\0":
        raise ValueError("input is not a WebAssembly 1.0 module")
    pos = 8
    while pos < len(wasm):
        section_id = wasm[pos]
        size, payload_start = read_u32(wasm, pos + 1)
        payload_end = payload_start + size
        if payload_end > len(wasm):
            raise ValueError("section extends beyond end of WASM file")
        yield section_id, wasm[payload_start:payload_end]
        pos = payload_end

def rewrite_memory_section(payload, requested_min_pages):
    count, pos = read_u32(payload, 0)
    memories = []
    for _ in range(count):
        flags, pos = read_u32(payload, pos)
        # MVP: bit 0 means a maximum is present; bit 1 means shared;
        # bit 2 means memory64. Do not rewrite unsupported memory types.
        if flags & ~0x7 or flags & 0x6:
            raise ValueError(f"unsupported memory flags: {flags}")
        minimum, pos = read_u32(payload, pos)
        maximum = None
        if flags & 1:
            maximum, pos = read_u32(payload, pos)
            if maximum < minimum:
                raise ValueError("memory maximum is smaller than minimum")
        new_minimum = min(minimum, requested_min_pages)
        memories.append((flags, minimum, new_minimum, maximum))
    if pos != len(payload):
        raise ValueError("unexpected trailing data in memory section")
    if not memories:
        raise ValueError("WASM memory section contains no memories")

    encoded = bytearray(write_u32(count))
    for flags, old_minimum, new_minimum, maximum in memories:
        encoded += write_u32(flags)
        encoded += write_u32(new_minimum)
        if flags & 1:
            encoded += write_u32(maximum)
    return bytes(encoded), memories

def build_variant(original, requested_min_pages):
    rebuilt = bytearray(original[:8])
    found_memory_section = False
    details = []
    for section_id, payload in parse_sections(original):
        if section_id == 5:
            if found_memory_section:
                raise ValueError("duplicate memory section")
            found_memory_section = True
            payload, details = rewrite_memory_section(payload, requested_min_pages)
        rebuilt.append(section_id)
        rebuilt += write_u32(len(payload))
        rebuilt += payload
    if not found_memory_section:
        raise ValueError("module has no defined memory section")
    return bytes(rebuilt), details

def main():
    if not SOURCE.is_file():
        raise FileNotFoundError(f"missing source build: {SOURCE}")
    OUT.mkdir(parents=True, exist_ok=True)
    compressed = SOURCE.read_bytes()
    original = gzip.decompress(compressed)
    report = {
        "source": str(SOURCE),
        "source_compressed_bytes": len(compressed),
        "source_wasm_bytes": len(original),
        "candidates": [],
    }

    for mib in CANDIDATE_MIB:
        requested_pages = mib * 1024 * 1024 // PAGE_SIZE
        candidate, memories = build_variant(original, requested_pages)
        filename = f"w-pt.initial-{mib}MiB.wasm.unityweb"
        target = OUT / filename
        target.write_bytes(gzip.compress(candidate, compresslevel=6, mtime=0))
        report["candidates"].append({
            "requested_initial_mib": mib,
            "file": str(target),
            "compressed_bytes": target.stat().st_size,
            "wasm_bytes": len(candidate),
            "memories": [{
                "original_min_pages": old,
                "new_min_pages": new,
                "maximum_pages": maximum,
            } for flags, old, new, maximum in memories],
            "warning": "Structural patch only; runtime compatibility is not established.",
        })

    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
