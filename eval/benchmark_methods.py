"""Produces the numbers behind the "pragmatic approach" table in the
README: measured accuracy and latency for each classification tier (rules,
TF-IDF, LLM) over every labeled document in eval/golden_dataset and
eval/real_samples, run independently of the cascade in classify.py so each
tier is scored on the full set rather than only the documents that reached it.

    python eval/benchmark_methods.py

Also times the OCR tiers (Tesseract vs. VLM) on whatever scanned images are
available in those two directories, since a fair comparison there needs the
same input fed to both engines rather than only the one the cascade picked.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from docket.classify import classify_llm, classify_rules  # noqa: E402
from docket.classify_tfidf import classify_tfidf  # noqa: E402
from docket.llm_client import LLMError, vision_transcribe  # noqa: E402
from docket.ocr import _ocr_image  # noqa: E402

ROOT = Path(__file__).parent
DIRS = [ROOT / "golden_dataset", ROOT / "real_samples"]


def _labeled_texts() -> list[tuple[str, str]]:
    """(text, doc_type) pairs for every document that has usable text —
    i.e. everything except the pure-image scans, which classification never
    sees raw pixels for anyway (OCR/VLM already turned them into text by
    the time classify() runs).
    """
    pairs = []
    for d in DIRS:
        if not d.exists():
            continue
        for expected_path in sorted(d.glob("*.expected.json")):
            expected = json.loads(expected_path.read_text())
            doc_type = expected.get("doc_type")
            if doc_type is None:
                continue
            text_path = expected_path.with_suffix("").with_suffix(".txt")
            if not text_path.exists():
                continue
            pairs.append((text_path.read_text(), doc_type))
    return pairs


def _bench_classifier(name: str, fn, pairs: list[tuple[str, str]]) -> dict:
    correct = 0
    attempted = 0
    latencies = []
    for text, expected_type in pairs:
        start = time.perf_counter()
        result = fn(text)
        latencies.append(time.perf_counter() - start)
        if result is None:
            continue
        attempted += 1
        if result.doc_type.value == expected_type:
            correct += 1
    n = len(pairs)
    return {
        "method": name,
        "n": n,
        "answered": attempted,
        "accuracy_on_answered": round(correct / attempted, 3) if attempted else None,
        "accuracy_on_all": round(correct / n, 3) if n else None,
        "mean_latency_ms": round(statistics.mean(latencies) * 1000, 1) if latencies else None,
    }


def _bench_ocr(images: list[Path]) -> list[dict]:
    """Time both text-acquisition tiers on the same images.

    A vision call timing out is an ordinary event on a laptop, not a reason
    to lose the whole benchmark: an earlier version let the exception escape
    and the run died after the classification numbers had been computed but
    before anything was written, so nothing survived.
    """
    from PIL import Image

    rows = []
    for path in images:
        image = Image.open(path)

        start = time.perf_counter()
        tess_text, tess_conf, _witness = _ocr_image(image)
        tess_latency = time.perf_counter() - start

        row = {
            "image": path.name,
            "tesseract_confidence": round(tess_conf, 1),
            "tesseract_chars": len(tess_text.strip()),
            "tesseract_latency_s": round(tess_latency, 2),
        }

        start = time.perf_counter()
        try:
            vlm_text = vision_transcribe(str(path))
        except LLMError as exc:
            row["vlm_error"] = f"{type(exc).__name__}: {exc}"
            row["vlm_latency_s"] = round(time.perf_counter() - start, 2)
            print(f"    skipped VLM on {path.name}: {exc}")
        else:
            row["vlm_chars"] = len(vlm_text.strip())
            row["vlm_latency_s"] = round(time.perf_counter() - start, 2)

        rows.append(row)
    return rows


def main() -> None:
    pairs = _labeled_texts()
    print(f"Classification benchmark — {len(pairs)} labeled documents (golden_dataset + real_samples)\n")

    rows = [
        _bench_classifier("rules", classify_rules, pairs),
        _bench_classifier("tfidf", classify_tfidf, pairs),
        _bench_classifier("llm", lambda t: classify_llm(t), pairs),
    ]
    print(f"{'method':<8} {'n':<4} {'answered':<9} {'acc(answered)':<14} {'acc(all)':<9} {'latency_ms'}")
    for r in rows:
        print(
            f"{r['method']:<8} {r['n']:<4} {r['answered']:<9} "
            f"{r['accuracy_on_answered']!s:<14} {r['accuracy_on_all']!s:<9} {r['mean_latency_ms']}"
        )

    # Write the classification numbers before touching the models again, so
    # a slow or unreachable vision model can't cost you results you already
    # have. The OCR section then updates the same file.
    out = ROOT / "results" / "benchmark_methods.json"
    out.parent.mkdir(exist_ok=True)
    report: dict = {"classification": rows}
    out.write_text(json.dumps(report, indent=2))

    image_paths = sorted(p for d in DIRS if d.exists() for p in d.glob("*.png"))
    if image_paths:
        print(f"\nOCR benchmark — {len(image_paths)} scanned image(s)\n")
        report["ocr"] = _bench_ocr(image_paths)
        for r in report["ocr"]:
            print(f"    {r}")
        out.write_text(json.dumps(report, indent=2))
    else:
        print("\nNo scanned images found for the OCR benchmark.")

    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
