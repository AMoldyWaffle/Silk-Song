#!/usr/bin/env python3
"""Read-only inventory of Unity WebGL Addressables bundles."""
import csv
import gc
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

import requests
import UnityPy

REPO = os.environ.get("GITHUB_REPOSITORY", "AMoldyWaffle/Silk-Song")
REF = os.environ.get("GITHUB_REF_NAME", "main")
API = "https://api.github.com/repos/" + REPO + "/contents/StreamingAssets/aa/WebGL?ref=" + REF
OUT = Path("audit-report")
OUT.mkdir(exist_ok=True)
S = requests.Session()
S.headers.update({"Accept": "application/vnd.github+json", "User-Agent": "Silk-Song-Asset-Audit"})

def mib(n):
    return f"{n / 1048576:.2f} MiB"

def integer(v):
    try:
        return int(v or 0)
    except Exception:
        return 0

def texture_bytes(w, h, fmt, mip):
    f = str(fmt).lower()
    px = max(0, w) * max(0, h)
    if any(x in f for x in ("dxt1", "bc1", "etc1", "etc_rgb4")):
        n = ((w + 3)//4) * ((h + 3)//4) * 8
    elif any(x in f for x in ("dxt5", "bc3", "bc5", "bc7", "etc2", "astc")):
        n = ((w + 3)//4) * ((h + 3)//4) * 16
    elif any(x in f for x in ("rgba4444", "argb4444", "rgb565", "r16")):
        n = px * 2
    elif "r8" in f or "alpha8" in f:
        n = px
    elif "rgba64" in f or "rgba_half" in f:
        n = px * 8
    else:
        n = px * 4
    return int(n * (4/3 if mip else 1))

def main():
    r = S.get(API, timeout=60)
    r.raise_for_status()
    bundles = [x for x in r.json() if x.get("type") == "file" and x.get("name", "").endswith(".bundle")]
    if not bundles:
        raise RuntimeError("No AssetBundles found")
    rows, failures = [], []
    totals, types = Counter(), Counter()
    print(f"Found {len(bundles)} bundles; processing one at a time.", flush=True)

    for i, item in enumerate(bundles, 1):
        row = {
            "bundle": item["name"], "compressed_bytes": integer(item.get("size")),
            "asset_count": 0, "texture_count": 0, "texture_estimated_gpu_bytes": 0,
            "texture_source_bytes": 0, "audio_count": 0, "audio_source_bytes": 0,
            "audio_estimated_pcm_bytes": 0, "other_objects": 0, "parse_error": ""
        }
        temp_path = None
        try:
            print(f"[{i}/{len(bundles)}] {item['name']}", flush=True)
            with S.get(item["download_url"], stream=True, timeout=(30, 180)) as response:
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(delete=False, suffix=".bundle") as f:
                    temp_path = f.name
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            f.write(chunk)
            env = UnityPy.load(temp_path)
            for obj in env.objects:
                row["asset_count"] += 1
                typ = getattr(obj.type, "name", str(obj.type))
                types[typ] += 1
                if typ == "Texture2D":
                    row["texture_count"] += 1
                    try:
                        d = obj.read()
                        w, h = integer(getattr(d, "m_Width", 0)), integer(getattr(d, "m_Height", 0))
                        fmt = getattr(d, "m_TextureFormat", "unknown")
                        mip = bool(getattr(d, "m_MipMap", False))
                        raw = getattr(d, "image_data", b"") or b""
                        row["texture_source_bytes"] += len(raw)
                        row["texture_estimated_gpu_bytes"] += texture_bytes(w, h, fmt, mip)
                    except Exception:
                        pass
                elif typ == "AudioClip":
                    row["audio_count"] += 1
                    try:
                        d = obj.read()
                        raw = getattr(d, "m_AudioData", b"") or b""
                        row["audio_source_bytes"] += len(raw)
                        samples = integer(getattr(d, "m_Samples", 0))
                        channels = max(1, integer(getattr(d, "m_Channels", 1)))
                        row["audio_estimated_pcm_bytes"] += samples * channels * 4
                    except Exception:
                        pass
                else:
                    row["other_objects"] += 1
            del env
        except Exception as e:
            row["parse_error"] = (type(e).__name__ + ": " + str(e))[:500]
            failures.append({"bundle": item["name"], "error": row["parse_error"]})
            print("ERROR " + row["parse_error"], flush=True)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
            rows.append(row)
            for k in ("compressed_bytes", "asset_count", "texture_count",
                      "texture_estimated_gpu_bytes", "texture_source_bytes",
                      "audio_count", "audio_source_bytes", "audio_estimated_pcm_bytes",
                      "other_objects"):
                totals[k] += row[k]
            gc.collect()

    rows.sort(key=lambda x: x["compressed_bytes"], reverse=True)
    report = {
        "repository": REPO, "bundle_count": len(bundles), "totals": dict(totals),
        "object_types": dict(types), "failures": failures, "bundles": rows,
        "notes": [
            "Texture GPU sizes are estimates, not measured browser allocations.",
            "Audio PCM estimate assumes float32 decoded samples; Unity load behavior varies.",
            "Compressed bundle size is not runtime RAM.",
            "Read-only audit; no game assets are changed."
        ]
    }
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (OUT / "bundles.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# Unity Asset Memory Audit", "",
        f"Bundles inspected: {len(bundles)}",
        f"Compressed bundle total: {mib(totals['compressed_bytes'])}",
        f"Texture2D objects: {totals['texture_count']}",
        f"Estimated texture allocation: {mib(totals['texture_estimated_gpu_bytes'])}",
        f"AudioClip objects: {totals['audio_count']}",
        f"Estimated decoded float32 audio: {mib(totals['audio_estimated_pcm_bytes'])}",
        f"Parse failures: {len(failures)}", "",
        "| Bundle | Compressed | Textures | Est. texture RAM | Audio | Est. PCM |",
        "|---|---:|---:|---:|---:|---:|"
    ]
    for x in rows[:35]:
        lines.append(f"| {x['bundle']} | {mib(x['compressed_bytes'])} | {x['texture_count']} | {mib(x['texture_estimated_gpu_bytes'])} | {x['audio_count']} | {mib(x['audio_estimated_pcm_bytes'])} |")
    lines += ["", "## Object types", ""]
    lines += [f"- {k}: {v}" for k, v in types.most_common()]
    if failures:
        lines += ["", "## Failures", ""]
        lines += [f"- {x['bundle']}: {x['error']}" for x in failures]
    lines += ["", "These figures prioritize likely asset hotspots. They do not prove which assets remain resident in a gameplay session. Changing texture/audio import settings or WebAssembly initial memory usually requires rebuilding the Unity player and should not be done by blindly deleting bundles or editing the WASM binary.", ""]
    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print("Audit complete.", flush=True)

if __name__ == "__main__":
    main()
