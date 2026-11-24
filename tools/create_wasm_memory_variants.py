#!/usr/bin/env python3
"""Create experimental lower-initial-memory copies of a Unity WebGL WASM build.

The original .unityweb file is never modified. Candidates preserve the declared
maximum memory and only lower the WebAssembly memory-section minimum.
"""
import gzip
import json
import struct
from pathlib import Path

SOURCE = Path("Build/w-pt.wasm.unityweb")
OUT = Path("wasm-memory-experiments")
CANDIDATE_MIB = (512, 768, 1024, 1536)
PAGE = 65536

def read_u32(data, pos):
    value = shift = 0
    while True:
        b = data[pos]
        pos += 1
        value |= (b & 0x7f) << shift
        if not b & 0x80:
            return value, pos
        shift += 7
        if shift > 35:
            raise ValueError("invalid ULEB128")

def write_u32(value):
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7f) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)

def sections(wasm):
    if wasm[:8] != b"\0asm\x01\0\0\0":
        raise ValueError("not a WebAssembly 1.0 module")
    pos = 8
    while pos < len(wasm):
        sid = wasm[pos]
        size, start = read_u32(wasm, pos + 1)
        end = start + size
        if end > len(wasm):
            raise ValueError("section exceeds module length")
        yield sid, wasm[pos + 1:start], wasm[start:end]
        pos = end

def rewrite_memory_section(payload, new_min_pages):
    count, pos = read_u32(payload, 0)
    memories = []
    changed = False
    for _ in range(count):
        flags, pos = read_u32(payload, pos)
        minimum, pos = read_u32(payload, pos)
        maximum = None
        if flags & 1:
            maximum, pos = read_u32(payload, pos)
        if flags & 2:
            raise ValueError("shared memory is not supported by this patcher")
        replacement = min(minimum, new_min_pages)
        changed |= replacement != minimum
        memories.append((flags, replacement, maximum, minimum))
    if pos != len(payload):
        raise ValueError("unexpected trailing bytes in memory section")
    out = bytearray(write_u32(count))
    for flags, minimum, maximum, original_min in memories:
        out += write_u32(flags) + write_u32(minimum)
        if flags & 1:
            out += write_u32(maximum)
    return bytes(out), memories, changed

def main():
    OUT.mkdir(exist_ok=True)
    compressed = SOURCE.read_bytes()
    original = gzip.decompress(compressed)
    report = {"source": str(SOURCE), "source_compressed_bytes": len(compressed),
              "source_wasm_bytes": len(original), "candidates": []}
    parsed = list(sections(original))
    if sum(1 for sid, _, _ in parsed if sid == 5) != 1:
        raise ValueError("expected exactly one WASM memory section")

    for mib in CANDIDATE_MIB:
        target_pages = mib * 1024 * 1024 // PAGE
        rebuilt = bytearray(original[:8])
        memory_info = None
        changed = False
        for sid, size_bytes, payload in parsed:
            if sid == 5:
                payload, memories, changed = rewrite_memory_section(payload, target_pages)
                memory_info = [{"flags": f, "original_min_pages": old,
                                "new_min_pages": new, "maximum_pages": maximum}
                               for f, new, maximum, old in memories]
            rebuilt.append(sid)
            rebuilt += write_u32(len(payload))
            rebuilt += payload
        if not changed:
            report["candidates"].append({"initial_mib": mib, "status": "unchanged"})
            continue
        name = f"w-pt.initial-{mib}MiB.wasm.unityweb"
        target = OUT / name
        target.write_bytes(gzip.compress(bytes(rebuilt), compresslevel=6, mtime=0))
        report["candidates"].append({
            "initial_mib": mib, "status": "created",
            "file": str(target), "compressed_bytes": target.stat().st_size,
            "memory": memory_info,
            "warning": "Structural patch only; must be tested in the actual Unity runtime."
        })
    (OUT / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
