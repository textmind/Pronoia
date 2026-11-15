"""Export real experiment observations; gaps and regressions stay visible."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from .corpus import atomic_json


def render_report(root: Path, *, baseline, history, final=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    points = [{"round": row["round"], "version": row["version"], "validation_accuracy": (row.get("validation") or {}).get("score"),
               "learning_accuracy": (row.get("learning") or {}).get("score"), "best_accuracy": row["best_accuracy"],
               "accepted": row["accepted"], "baseline_accuracy": baseline["score"], "skipped": row.get("skipped")} for row in history]
    atomic_json(root / "curve.json", {"points": points, "baseline": baseline, "test": (final or {}).get("test"),
        "note": "Validation best-so-far is a selection statistic, not proof of generalization. Missing candidates are gaps, not interpolated improvements."})
    csv_path = root / "accuracy.csv.tmp"
    with csv_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(points[0]) if points else ["round"])
        writer.writeheader()
        writer.writerows(points)
    csv_path.replace(root / "accuracy.csv")
    figure, axes = plt.subplots(1, 2 if final else 1, figsize=(12 if final else 8, 4.5), squeeze=False)
    ax = axes[0, 0]
    rounds = [p["round"] for p in points]
    ax.plot(rounds, [float("nan") if p["validation_accuracy"] is None else p["validation_accuracy"] * 100 for p in points], "o-", color="#ab6b3b", label="Candidate / validation")
    ax.plot(rounds, [p["best_accuracy"] * 100 for p in points], "s-", color="#287b68", label="Incumbent / validation")
    ax.axhline(baseline["score"] * 100, color="#707579", linestyle="--", label="Pre-integration baseline")
    rejected = [p for p in points if not p["accepted"] and p["validation_accuracy"] is not None]
    ax.scatter([p["round"] for p in rejected], [p["validation_accuracy"] * 100 for p in rejected], marker="x", color="#b3403e", s=70, zorder=4, label="Not promoted")
    ax.set(xlabel="Evolution round", ylabel="Fixed-denominator accuracy (%)", title="Measured validation trajectory", xticks=rounds)
    ax.set_ylim(0, 100)
    ax.grid(alpha=.2)
    ax.legend(fontsize=8)
    if final:
        ax2 = axes[0, 1]
        rows = list(final["test"].items())
        ax2.bar([k for k, _ in rows], [v["score"] * 100 for _, v in rows], color=["#707579"] + ["#287b68"] * (len(rows) - 1))
        reference = max(final["test"]["baseline"]["always_up_accuracy"], final["test"]["baseline"]["always_down_accuracy"])
        ax2.axhline(reference * 100, linestyle=":", color="#b3403e", label="Constant-direction reference")
        ax2.set(ylabel="Accuracy (%)", title="Sealed, post-selection test")
        ax2.set_ylim(0, 100)
        ax2.grid(axis="y", alpha=.2)
        ax2.legend(fontsize=8)
    figure.tight_layout()
    for extension in ("png", "svg"):
        temporary = root / f"accuracy.tmp.{extension}"
        figure.savefig(temporary, dpi=180)
        temporary.replace(root / f"accuracy.{extension}")
    plt.close(figure)
    def pct(value):
        return "—" if value is None else f"{value * 100:.1f}%"

    rows = "".join("<tr>" + "".join("<td>" + html.escape(str(value)) + "</td>" for value in (
        p["round"], p["version"], pct(p["learning_accuracy"]), pct(p["validation_accuracy"]), pct(p["best_accuracy"]),
        "接受" if p["accepted"] else "未晋升", p.get("skipped") or "—")) + "</tr>" for p in points)
    evidence = "真实模型回测进行中；最终测试集尚未评分。" if final is None else (
        "本次注册的配对测试支持改善；仍需后续独立样本验证。" if final["improvement_proven"] else "本次尚未证明稳定的测试集改善。")
    test_section = ""
    if final:
        test_rows = "".join("<tr>" + "".join("<td>" + html.escape(str(value)) + "</td>" for value in (
            version, metric["correct"], metric["n"], pct(metric["score"]), pct(metric["coverage"]), pct(metric.get("balanced_accuracy")),
            pct(metric.get("always_up_accuracy")), pct(metric.get("always_down_accuracy")))) + "</tr>"
            for version, metric in final["test"].items())
        paired = html.escape(json.dumps(final.get("paired_test_comparison"), ensure_ascii=False, indent=2))
        gates = html.escape(json.dumps(final.get("quality_gates"), ensure_ascii=False, indent=2))
        benchmark = html.escape(json.dumps(final.get("official_evolver_benchmark"), ensure_ascii=False, indent=2))
        test_section = f"""<h2>封存后的独立测试</h2><p>验证集所选版本：{html.escape(final['chosen'])}。比较版本与预测均在测试评分前锁定；测试结果不参与本次继续优化。</p>
<p>{final.get('unique_test_n', next(iter(final['test'].values()))['n'])} 个唯一事件，每臂重复 {final.get('repetitions', 1)} 次；重复运行只用于稳定性估计，不作为额外独立样本。</p>
<table><tr><th>版本</th><th>正确</th><th>样本</th><th>准确率</th><th>覆盖率</th><th>平衡准确率</th><th>固定看涨</th><th>固定看跌</th></tr>{test_rows}</table>
<details><summary>配对变化与14天块不确定性</summary><pre>{paired}</pre></details>
<details><summary>改善判定门槛</summary><pre>{gates}</pre></details>
<details><summary>Evolver 官方配对基准统计</summary><pre>{benchmark}</pre></details>"""
    document = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Pronoia · EvoMap 回测进化</title>
<style>body{{font:16px/1.65 system-ui;max-width:1200px;margin:40px auto;background:#f7f5ef;color:#292b29;padding:20px}}img{{width:100%}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}td,th{{padding:10px;border-bottom:1px solid #d7d4ca;text-align:left}}pre{{overflow:auto;font-size:13px}}details{{margin-top:20px}}</style>
<h1>Pronoia × EvoMap · 准确率曲线</h1><p>{html.escape(evidence)}</p><p>基线验证样本 {baseline['n']} 条；主指标固定分母，缺失、无效与弃权均不算命中。候选的退化与跳过轮次完整保留。验证集最佳曲线体现选优过程，不代表未来准确率单调上升。</p>
<img src="accuracy.svg" alt="实际验证与测试准确率，纵轴0至100%"><div class="scroll"><table><tr><th>轮次</th><th>版本</th><th>学习集</th><th>验证集</th><th>当前最佳</th><th>晋升</th><th>跳过原因</th></tr>{rows}</table></div>
<div class="scroll">{test_section}</div><p><a href="accuracy.csv">验证轨迹 CSV</a> · <a href="curve.json">曲线 JSON</a> · <a href="accuracy.png">PNG</a></p></html>"""
    path = root / "report.html.tmp"
    path.write_text(document)
    path.replace(root / "report.html")
