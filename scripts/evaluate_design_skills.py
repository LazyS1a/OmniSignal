"""Bounded self-owned synthetic evaluation; default only creates the manifest.

Real calls require --allow-paid. Every submission is recorded before execution;
reruns never retry a reserved, failed, interrupted, or completed request. This is
a technical image-routing check, not evidence of real-world design quality.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw, ImageFont
from omnisignal.knowledge.release import _lock
from omnisignal.workbench.analysis import AnalysisService, HTTPAnalysisProvider
from omnisignal.workbench.design_skills import PROFILES, versions
from omnisignal.workbench.store import Conflict, Store, require

FIXTURE_VERSION = "design-self-owned-1"
MAX_CALLS = 40


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def draw_fixture(profile, variant):
    """Programmatically drawn source images; no external material or alteration."""
    colors = ["#258467", "#2d65aa", "#ab543d", "#71549e", "#b38431"]
    accent = colors[variant]
    image = Image.new("RGB", (768, 640), "#f2efea")
    draw = ImageDraw.Draw(image)
    try:
        title = ImageFont.load_default(size=40)
        text = ImageFont.load_default(size=22)
    except TypeError:
        title = text = ImageFont.load_default()
    if profile == "packaging-analysis":
        for i in range(3):
            x = 115 + i * 185
            draw.rectangle((x, 150, x + 145, 500), fill=accent, outline="#ffffff", width=4)
            draw.polygon([(x, 150), (x + 20, 120), (x + 165, 120), (x + 145, 150)], fill="#b6b2ab")
            draw.polygon([(x + 145, 150), (x + 165, 120), (x + 165, 470), (x + 145, 500)], fill="#666666")
            draw.text((x + 15, 180), "LEAF", font=text, fill="white")
            draw.text((x + 15, 230), "TEA", font=title, fill="white")
            draw.text((x + 15, 420), f"BLEND {i + variant + 1}", font=text, fill="white")
    elif profile == "brand-visual-analysis":
        draw.text((70, 45), "LUMA", font=title, fill=accent)
        draw.ellipse((70, 115, 180, 225), fill=accent)
        draw.rectangle((205, 115, 650, 280), fill="white")
        draw.text((245, 165), "LUMA", font=title, fill=accent)
        draw.rectangle((70, 325, 390, 580), fill="white", outline=accent, width=3)
        draw.text((95, 350), "LUMA", font=text, fill=accent)
        draw.line((95, 405, 355, 405), fill=accent, width=3)
        draw.rectangle((440, 325, 660, 455), fill=accent)
        draw.text((465, 370), "LUMA", font=text, fill="white")
        for i, color in enumerate((accent, "#272727", "#c6c4c0")):
            draw.rectangle((440 + i * 75, 500, 495 + i * 75, 555), fill=color)
    elif profile == "static-ad-analysis":
        draw.rectangle((40, 40, 728, 600), fill=accent)
        draw.text((75, 70), "WEEKEND SALE", font=title, fill="white")
        draw.text((75, 135), "SAVE 20%", font=title, fill="white")
        draw.ellipse((270, 230, 610, 410), fill="#ffffff")
        draw.rectangle((270, 310, 610, 405), fill="#eeeeee")
        draw.text((330, 300), "SNEAKER", font=text, fill=accent)
        draw.rectangle((75, 470, 310, 540), fill="#ffffff")
        draw.text((100, 490), "SHOP NOW", font=text, fill=accent)
    else:
        # Intentionally ambiguous abstract studies without a product/brand claim.
        for i in range(5):
            x = 70 + i * 125
            y = 110 + (i * 47 + variant * 19) % 250
            draw.ellipse((x, y, x + 80, y + 90), fill=accent)
            draw.line((x, y + 100, x + 95, 530 - i * 15), fill="#555555", width=2)
    draw.text((30, 610), f"SELF-OWNED SYNTHETIC {variant + 1}", font=text, fill="#555555")
    return image


def prepare(output):
    output.mkdir(parents=True, exist_ok=True)
    path = output / "manifest.json"
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        require(manifest.get("fixture_version") == FIXTURE_VERSION, "Existing fixture version differs; use another output directory")
        require(len(manifest.get("samples", [])) == 20, "Incomplete evaluation manifest")
        for sample in manifest["samples"]:
            image_path = output / sample["path"]
            require(image_path.resolve().is_relative_to(output.resolve()) and image_path.is_file(), "Invalid fixture path")
            require(hashlib.sha256(image_path.read_bytes()).hexdigest() == sample["sha256"], "Fixture image changed; evaluation halted")
        for index, sample in enumerate(manifest["samples"]):
            require(sample["id"] == f"sample-{index + 1:02d}" and sample["expected_profile"] == list(PROFILES)[index // 5]
                    and sample["split"] == ("development" if index % 5 < 3 else "holdout")
                    and sample.get("rights") == "self_owned_fixture", "Fixture labels/split changed; evaluation halted")
        return manifest
    images = output / "images"
    images.mkdir(exist_ok=True)
    samples = []
    for group, profile in enumerate(PROFILES):
        for variant in range(5):
            identifier = f"sample-{group * 5 + variant + 1:02d}"
            image_path = images / f"{identifier}.png"
            draw_fixture(profile, variant).save(image_path, "PNG")
            samples.append({"id": identifier, "expected_profile": profile, "split": "development" if variant < 3 else "holdout",
                            "path": f"images/{identifier}.png", "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                            "rights": "self_owned_fixture", "limitations": "程序绘制技术样本；不代表真实案例分析质量"})
    manifest = {"fixture_version": FIXTURE_VERSION, "schema_version": 1, "samples": samples,
                "development_count": 12, "holdout_count": 8, "maximum_paid_submissions": MAX_CALLS,
                "quality_scope": "technical_synthetic_only", "human_quality_reviewed": None}
    write_json(path, manifest)
    return manifest


@contextmanager
def isolated_limits():
    keys = {"OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT": "40", "OMNISIGNAL_WORKBENCH_DESIGN_SKILLS_DAILY_LIMIT": "40"}
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def provider_factory(lane):
    return HTTPAnalysisProvider(api_key=os.getenv("OMNISIGNAL_WORKBENCH_MODEL_API_KEY", ""),
        model=os.getenv("OMNISIGNAL_WORKBENCH_MODEL", ""),
        base_url=os.getenv("OMNISIGNAL_WORKBENCH_MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        design_skills=lane == "skills")


def summarize(manifest, ledger):
    attempts = ledger["attempts"]
    counts = {status: sum(a.get("status") == status for a in attempts.values()) for status in ("completed", "failed", "interrupted", "stale", "reserved", "queued")}
    groups = {}
    for split in ("development", "holdout"):
        samples = [s for s in manifest["samples"] if s["split"] == split]
        rows = [attempts.get(s["id"] + ":skills", {}) for s in samples]
        groups[split] = {"planned": len(samples), "completed": sum(x.get("status") == "completed" for x in rows),
                         "profile_hits": sum(x.get("profile_hit") is True for x in rows),
                         "profile_misses": sum(x.get("profile_hit") is False for x in rows),
                         "unknown": sum(x.get("profile_hit") is None for x in rows)}
    return {"fixture_version": manifest["fixture_version"], "quality_scope": manifest["quality_scope"], "versions": versions(),
            "reserved_submissions": len(attempts), "maximum_paid_submissions": MAX_CALLS, "states": counts, "splits": groups,
            "human_quality_reviewed": None, "cost": None, "cost_note": "价格未配置；token usage 按实际响应记录，不能由token数捏造金额。",
            "uncertain_submission_blocks_new_calls": any(x.get("status") in {"reserved", "queued", "running", "waiting_remote"} for x in attempts.values()),
            "cases": [{**sample, "runs": {lane: attempts.get(sample["id"] + ":" + lane) for lane in ("legacy", "skills")}} for sample in manifest["samples"]]}


def evaluate(output, *, allow_paid=False, max_calls=MAX_CALLS, factory=provider_factory):
    output = Path(output).resolve()
    require(type(max_calls) is int and 1 <= max_calls <= MAX_CALLS, "max_calls must be 1..40")
    output.mkdir(parents=True, exist_ok=True)
    with _lock(output / ".evaluation"):
        manifest = prepare(output)
        ledger_path = output / "ledger.json"
        require(ledger_path.exists() or not any((output / ("store-" + lane)).exists() for lane in ("legacy", "skills")), "Ledger missing beside existing evaluation stores; stop without resubmission")
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {"fixture_version": FIXTURE_VERSION, "attempts": {}}
        require(ledger.get("fixture_version") == FIXTURE_VERSION and isinstance(ledger.get("attempts"), dict), "Invalid evaluation ledger")
        pending_previous = any(x.get("status") in {"reserved", "queued", "running", "waiting_remote"} for x in ledger["attempts"].values())
        if allow_paid and not pending_previous:
            with isolated_limits():
                lanes = {}
                for lane in ("legacy", "skills"):
                    provider = factory(lane)
                    # Separate stores prevent rollout cache reuse from disguising
                    # a baseline comparison. These are new synthetic tasks only.
                    store = Store(output / ("store-" + lane), recover_publications=False)
                    service = AnalysisService(store, provider)
                    lanes[lane] = (provider, store, service)
                for sample in manifest["samples"]:
                    for lane, (provider, store, service) in lanes.items():
                        key = sample["id"] + ":" + lane
                        previous = ledger["attempts"].get(key)
                        if previous is not None:
                            require(previous["provider_identity"] == provider.cache_identity, "Provider/rules changed; preserve this evaluation and use a new directory")
                            # Never run_pending here: a previous reservation may
                            # have crossed the billable boundary before a crash.
                            if previous.get("job_id"):
                                jobs = service.jobs(previous["asset_id"])
                                found = next((j for j in jobs if j["id"] == previous["job_id"]), None)
                                if found:
                                    previous["status"] = found["status"]
                            continue
                        if len(ledger["attempts"]) >= max_calls:
                            continue
                        asset = store.import_asset({"title": "自有技术样本 " + sample["id"], "description": "程序绘制的自有合成素材，仅用于技术评测，不代表真实案例。", "source_url": "https://example.invalid/design-evaluation/" + sample["id"]}, (output / sample["path"]).read_bytes())
                        asset = store.review(asset["id"], {"expected_revision": asset["revision"], "permissions": {"analysis": "allowed", "display": "unknown", "export": "unknown"}, "permission_note": "self_owned_fixture; isolated technical analysis only"}, "synthetic-evaluation", "admin")
                        attempt = {"status": "reserved", "provider_identity": provider.cache_identity, "asset_id": asset["id"], "job_id": None,
                                   "usage": None, "profile": None, "profile_hit": None, "quality_status": None, "issues": []}
                        ledger["attempts"][key] = attempt
                        write_json(ledger_path, ledger)
                        job = service.enqueue(asset["id"], asset["revision"], "synthetic-evaluation")
                        attempt.update(job_id=job["id"], status=job["status"])
                        write_json(ledger_path, ledger)
                        result = service.run_pending()
                        require(result is not None and result["id"] == job["id"], "Unexpected evaluation queue; stop without retry")
                        details = result.get("details") or {}
                        attempt.update(status=result["status"], failure_code=result.get("failure_code"), charged=result["charged"],
                                       usage=details.get("usage", getattr(provider, "last_usage", None)), profile=details.get("profile"),
                                       profile_hit=(details["profile"] == sample["expected_profile"]) if result["status"] == "completed" and details else None,
                                       quality_status=details.get("quality", {}).get("status"),
                                       issues=[{"code": issue["code"], "field": issue["field"]} for issue in details.get("quality", {}).get("issues", [])])
                        write_json(ledger_path, ledger)
        report = summarize(manifest, ledger)
        write_json(output / "report.json", report)
        (output / "report.md").write_text(
            "# 专项分析技术评测\n\n20 张程序绘制的自有样本：12 张开发集、8 张保留集。\n\n"
            f"本次累计预留 {report['reserved_submissions']}/40 次调用；完成 {report['states']['completed']}；失败 {report['states']['failed']}。\n\n"
            "结果只说明合成图的技术流程和路由表现，不能代替真实案例的质量验收。人工质量审核量未知，成本金额未知。失败不自动重试。\n", encoding="utf-8")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/private/design-skills-evaluation"))
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--max-calls", type=int, default=MAX_CALLS)
    args = parser.parse_args()
    try:
        report = evaluate(args.output, allow_paid=args.allow_paid, max_calls=args.max_calls)
    except Exception:
        # Keep provider errors, local paths and material bodies out of stdout.
        print(json.dumps({"status": "stopped", "reason": "evaluation_guard_or_configuration_failure", "automatic_retry": False}))
        return 1
    print(json.dumps({key: report[key] for key in ("quality_scope", "reserved_submissions", "maximum_paid_submissions", "states", "splits")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
