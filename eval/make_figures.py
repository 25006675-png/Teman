"""Charts for the security report, from the evaluation results (eval/results/*.json).

Writes PNGs to docs/figures/. Run the evaluations first, then from the repo root:
    python eval/make_figures.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results"
OUT = ROOT / "docs" / "figures"

# validated categorical slots 1-3 (dataviz reference palette, light) + the status "critical" red
BLUE, ORANGE, AQUA, CRITICAL = "#2a78d6", "#eb6834", "#1baf7a", "#d03b3b"
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"

plt.rcParams.update({
    "font.family": ["DejaVu Sans", "Microsoft JhengHei"], "font.size": 10,  # JhengHei: Chinese labels "text.color": INK,
    "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "axes.titleweight": "bold",
    "axes.titlesize": 12, "axes.titlelocation": "left", "axes.titlepad": 30,
    "xtick.color": INK_2, "ytick.color": INK, "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "legend.frameon": False,
})


def load(name):
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def finish(ax, path, subtitle=None, note=None, note_y=-0.3):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    fig = ax.figure
    if subtitle:
        ax.text(0, 1.035, subtitle, transform=ax.transAxes, color=INK_2, fontsize=9.5, va="bottom")
    if note:  # below the legend, in the axes' own coordinates so it never collides with the axis label
        ax.text(0, note_y, note, transform=ax.transAxes, color=INK_2, fontsize=8, ha="left", va="top", wrap=True)
    fig.savefig(OUT / path, dpi=200, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    print("wrote", OUT / path)


def stacked(ax, labels, segments, total=None):
    """Horizontal stacked bars; segments = [(name, color, [values per row])]. 2px surface gaps, direct labels."""
    left = [0] * len(labels)
    for name, color, values in segments:
        bars = ax.barh(labels, values, left=left, color=color, height=0.55, edgecolor=SURFACE, linewidth=2, label=name)
        for bar, v in zip(bars, values):
            if v and bar.get_width() >= (total or 1) * 0.06:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_y() + bar.get_height() / 2, str(v),
                        ha="center", va="center", color="white" if color != AQUA else INK, fontsize=9.5, fontweight="bold")
            elif v:
                ax.text(bar.get_x() + bar.get_width() + (total or 1) * 0.012, bar.get_y() + bar.get_height() / 2, str(v),
                        ha="left", va="center", color=INK, fontsize=9.5, fontweight="bold")
        left = [a + b for a, b in zip(left, values)]
    ax.invert_yaxis()


# ---- 1. Scam test: where each request ended up ---------------------------------
def fig_scam():
    rows = load("scam_eval_results.json")
    def bucket(o):
        return {"APPROVE": "paid", "WARN": "paid", "REVIEW": "son", "BLOCK": "blocked"}.get(o, "asked")
    c = Counter((r["label"], bucket(r["rules_ai"]["outcome"])) for r in rows)
    labels = ["60 scam requests", "60 genuine requests"]
    fig, ax = plt.subplots(figsize=(7.5, 2.6))
    stacked(ax, labels, [
        ("Paid on her own tap", BLUE, [c[("scam", "paid")], c[("genuine", "paid")]]),
        ("Sent to her son to decide", ORANGE, [c[("scam", "son")], c[("genuine", "son")]]),
        ("Blocked outright", AQUA, [c[("scam", "blocked")], c[("genuine", "blocked")]]),
    ], total=60)
    ax.set_xlim(0, 62)
    ax.set_xlabel("requests")
    ax.legend(ncol=3, loc="upper left", bbox_to_anchor=(0, -0.32), fontsize=9)
    ax.set_title("No scam was paid: 60 of 60 stopped before money moved")
    finish(ax, "1-scam-outcomes.png", "Where each request ended up (rules + AI check). Synthetic test set, 120 requests.")

    review = [r for r in rows if r["rules_ai"]["outcome"] == "REVIEW"]
    share = {lab: (sum(1 for r in review if r["label"] == lab and r["rules_ai"]["ai_suspicious"]),
                   sum(1 for r in review if r["label"] == lab)) for lab in ("scam", "genuine")}
    fig, ax = plt.subplots(figsize=(7.5, 2.2))
    pct = [100 * share[k][0] / share[k][1] for k in ("scam", "genuine")]
    bars = ax.barh(["Scam requests", "Genuine requests"], pct, color=[ORANGE, BLUE], height=0.5)
    for bar, k in zip(bars, ("scam", "genuine")):
        ax.text(bar.get_width() + 1.5, bar.get_y() + bar.get_height() / 2,
                f"{share[k][0]} of {share[k][1]}  ({bar.get_width():.0f}%)", va="center", color=INK, fontsize=9.5)
    ax.set_xlim(0, 115); ax.set_xticks([0, 25, 50, 75, 100]); ax.set_xlabel("% of the son's review cards")
    ax.invert_yaxis()
    ax.set_title("The second AI tells her son which requests are scams")
    finish(ax, "2-ai-warning-on-son-cards.png", "Share of review cards that carry the AI's scam-tactic warning.")


# ---- 2. Speech: Cantonese understood ----------------------------------------------
def fig_speech():
    data = load("speech_eval_results.json")["summary"]
    kinds = [("command", "Voice commands\n(確認 / 取消)"), ("intent", "Everyday requests\n(balance, pay bill)"),
             ("amount", "Amounts\n(e.g. 四十二蚊五毫)")]
    fig, ax = plt.subplots(figsize=(7.5, 3.4))
    y = list(range(len(kinds)))
    for offset, cond, color, name in ((-0.17, "clean", BLUE, "Quiet room"), (0.17, "noisy", ORANGE, "Busy room (10 dB noise)")):
        vals = [data["by_kind"][k][cond] for k, _ in kinds]
        pct = [100 * v["correct"] / v["total"] for v in vals]
        bars = ax.barh([i + offset for i in y], pct, height=0.32, color=color, label=name, edgecolor=SURFACE, linewidth=2)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_width() + 1.2, bar.get_y() + bar.get_height() / 2, f"{v['correct']}/{v['total']}",
                    va="center", fontsize=9, color=INK)
    ax.set_yticks(y, [label for _, label in kinds])
    ax.set_xlim(0, 112); ax.set_xticks([0, 25, 50, 75, 100]); ax.set_xlabel("% understood correctly")
    ax.invert_yaxis()
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.2), ncol=2, fontsize=9)
    wrong = len(data["wrong_actions"])
    ax.set_title("Whisper understands spoken Cantonese commands and amounts")
    finish(ax, "3-speech-accuracy.png",
           f"Whisper large-v3 on {data['clips']} clips, 3 Hong Kong Cantonese voices. Wrong actions triggered: {wrong}.",
           "Synthetic computer voices, clearer than a 72-year-old speaker: a first result, not a test with real elders.", note_y=-0.34)


# ---- 3. Photos: robustness and safety -----------------------------------------------
def fig_photos():
    data = load("photo_eval_results.json")
    rows = data["rows"]
    variants = data["summary"]["variants"]
    def cls(r):
        if not r["safe"]:
            return "unsafe"
        if r["outcome"] == "DATA_INSUFFICIENT":
            return "asked"
        if all(r[f] in ("correct", "not on photo") for f in ("payee", "amount", "due_date")):
            return "correct"
        return "caught"
    c = Counter((r["variant"], cls(r)) for r in rows)
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    stacked(ax, variants, [
        ("Read correctly", BLUE, [c[(v, "correct")] for v in variants]),
        ("Couldn't read: asked her again", ORANGE, [c[(v, "asked")] for v in variants]),
        ("Misread, but outcome still safe", AQUA, [c[(v, "caught")] for v in variants]),
        ("Misread amount, payment allowed", CRITICAL, [c[(v, "unsafe")] for v in variants]),
    ], total=7)
    ax.set_xlim(0, 7); ax.set_xticks(range(8)); ax.set_xlabel("photos (of 7)")
    ax.legend(ncol=2, loc="upper left", bbox_to_anchor=(0, -0.13), fontsize=9)
    unsafe = data["summary"]["unsafe"]
    before = RESULTS / "photo_eval_results_before_fix.json"
    before_unsafe = len(json.loads(before.read_text(encoding="utf-8"))["summary"]["unsafe"]) if before.exists() else None
    ax.set_title(f"Bad photos: {len(rows) - len(unsafe)} of {len(rows)} handled safely")
    note = (f"Found and fixed: before the biller cross-check, {before_unsafe} tilted photos of a recognised biller's bill were misread\n"
            "with an amount still within limits. Teman now checks the amount against the bill the biller issued, and asks her\n"
            "to check when they differ (photo 6 shows an amount TNB never billed, so it is always asked again).") if before_unsafe else None
    finish(ax, "4-photo-robustness.png", "7 demo photos, each degraded 9 ways, through the real OCR and decision engine.",
           note, note_y=-0.33)


# ---- 4. Speed ---------------------------------------------------------------------
def fig_latency():
    data = load("latency_results.json")
    steps = data["steps"]
    fig, ax = plt.subplots(figsize=(7.5, 3.0))
    names = [s["step"] for s in steps]
    med = [s["median_s"] for s in steps]
    ax.barh(names, med, height=0.5, color=BLUE)
    ax.errorbar([s["p95_s"] for s in steps], names, fmt="|", color=INK_2, markersize=10, mew=1.5, label="95th percentile")
    for i, s in enumerate(steps):
        txt = ("under 1 ms" if s["median_s"] < 0.001 else f"{s['median_s'] * 1000:.0f} ms") if s["median_s"] < 1 else f"{s['median_s']:.1f} s"
        ax.text(max(s["p95_s"], s["median_s"]) + 0.15, i, txt, va="center", fontsize=9, color=INK)
    ax.set_xlabel("seconds (bar = median)")
    ax.set_xlim(0, max(s["p95_s"] for s in steps) * 1.18)
    ax.invert_yaxis()
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.24), fontsize=9)
    ax.set_title("Safety checks are instant; listening is the slow step")
    finish(ax, "5-latency.png", f"Measured on {data['machine']}.")


# ---- 5. Stress: one charge, no matter how many taps -----------------------------------
def fig_stress():
    s = load("stress_test_results.json")
    tiles = [
        (s["A_confirm_taps"]["requests"], "simultaneous taps\nby Ah Por", f"{s['A_confirm_taps']['charges']} charge"),
        (s["B_son_approvals"]["requests"], "simultaneous approvals\nby Ah Ming", f"{s['B_son_approvals']['charges']} charge"),
        (s["C_wrong_pins"]["requests"], "simultaneous\nwrong PINs", f"{s['C_wrong_pins']['charges']} charges,\npayment stopped"),
    ]
    fig = plt.figure(figsize=(7.5, 2.5))
    fig.text(0.02, 0.93, "Simultaneous requests never charge twice", fontsize=12, fontweight="bold", va="top")
    for i, (big, what, result) in enumerate(tiles):
        x = 0.02 + i * 0.33
        fig.text(x, 0.72, str(big), fontsize=26, fontweight="bold", color=INK, va="top")
        fig.text(x, 0.45, what, fontsize=9.5, color=INK_2, va="top", linespacing=1.3)
        fig.text(x, 0.2, "→ " + result, fontsize=10.5, fontweight="bold", color=INK, va="top", linespacing=1.3)
    fig.text(0.02, -0.08, "Real server, simultaneous HTTP requests; the balance was checked after each burst. "
             + ("All checks passed." if s["passed"] else "A CHECK FAILED."), color=INK_2, fontsize=8)
    fig.savefig(OUT / "6-stress-test.png", dpi=200, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    print("wrote", OUT / "6-stress-test.png")


# ---- 6. Automated tests by security property -------------------------------------------
GROUPS = [
    ("Signed approvals & family limits", ("test_mandates", "test_stage2_approval_contract")),
    ("Charged once, payment states, pay later", ("test_transactions", "test_pay_later")),
    ("Scam & risk rules", ("test_rules",)),
    ("AI guardrails (assistant, AI check, shadow model)", ("test_agent", "test_ai_and_community", "test_risk_model")),
    ("Family safeguards (PIN, own account, timer)", ("test_guardian_safeguards", "test_guardian_timer")),
    ("Required acceptance tests & demo flows", ("test_proposal_acceptance", "test_api", "test_lead_fixtures")),
    ("Reading input (photo, speech, voice output)", ("test_ocr", "test_ocr_contract", "test_bills", "test_photo_voice",
                                                     "test_speech_audio", "test_speech_commands", "test_speech_contract", "test_tts")),
    ("Budget, reports, evaluation tools", ("test_budget", "test_metrics", "test_check_references")),
]


def fig_tests():
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=ROOT, capture_output=True, text=True).stdout
    per_file = Counter(m.group(1) for m in re.finditer(r"^tests[/\\](\w+)\.py::", out, re.M))
    counts = [(name, sum(per_file[f] for f in files)) for name, files in GROUPS]
    other = sum(per_file.values()) - sum(n for _, n in counts)
    if other:
        counts.append(("Other", other))
    fig, ax = plt.subplots(figsize=(7.5, 3.6))
    bars = ax.barh([n for n, _ in counts], [v for _, v in counts], height=0.55, color=BLUE)
    for bar, (_, v) in zip(bars, counts):
        ax.text(bar.get_width() + 0.8, bar.get_y() + bar.get_height() / 2, str(v), va="center", fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("automated tests")
    ax.set_title(f"{sum(per_file.values())} automated tests, grouped by what they protect")
    finish(ax, "7-tests-by-property.png", "Every test runs on a temporary database and never calls the real AI.")


# ---- 8. Decision map: what the rules decide for every amount ------------------------------
# outcome bands are states, so they use the fixed status palette, always with a text label
BAND_STYLE = {
    "APPROVE": ("#0ca30c", INK, "Pays on her hold"),
    "WARN": ("#fab219", INK, "Warns; she can still pay"),
    "REVIEW": ("#ec835a", INK, "Her son must approve"),
    "BLOCK": (CRITICAL, "white", "Blocked; nobody can pay"),
}
BAND_SHORT = {"APPROVE": "Pay", "WARN": "Warn", "REVIEW": "Son", "BLOCK": "Blocked"}


def fig_decision_map():
    import math
    data = load("decision_map.json")
    rows = data["rows"]
    lo, hi = 1.0, data["max_minor"] / 100
    fig, ax = plt.subplots(figsize=(8.2, 4.4))
    for y, row in enumerate(rows):
        for seg in row["segments"]:
            start, end = max(seg["from_minor"] / 100, lo), seg["to_minor"] / 100
            if end <= lo:
                continue
            fill, ink, _ = BAND_STYLE[seg["band"]]
            ax.barh(y, end - start, left=start, height=0.62, color=fill, edgecolor=SURFACE, linewidth=2)
            if math.log10(end) - math.log10(start) > 0.17:
                score = "" if seg["band"] == "BLOCK" else f" · {float(seg['score']):g}"
                ax.text(math.sqrt(start * end), y, BAND_SHORT[seg["band"]] + score, ha="center", va="center",
                        color=ink, fontsize=8.5, fontweight="bold")
    ax.set_xscale("log")
    ax.set_xlim(lo, hi)
    ax.set_xticks([1, 10, 100, 500, 1000])
    ax.set_xticklabels(["RM 1", "RM 10", "RM 100", "RM 500", "RM 1,000"], fontsize=8.5)
    ax.minorticks_off()
    # the narrow warn band on the TNB row: label its edges above the bar
    ax.text(46.08, -0.42, "RM 46.08 ", ha="right", va="bottom", fontsize=7.5, color=INK_2)
    ax.text(57.60, -0.42, " RM 57.60", ha="left", va="bottom", fontsize=7.5, color=INK_2)
    ax.text(math.sqrt(46.09 * 57.60), -0.72, "Warn · 20", ha="center", va="bottom", fontsize=8, color=INK,
            fontweight="bold")
    ax.axvline(500, color=INK, linewidth=1, linestyle=(0, (3, 3)))
    ax.text(500, -0.72, "RM 500 hard cap ", ha="right", va="bottom", fontsize=8, color=INK)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r["label"] for r in rows], fontsize=9)
    ax.set_ylim(len(rows) - 0.4, -0.95)
    ax.set_xlabel("amount requested (log scale)")
    handles = [plt.Rectangle((0, 0), 1, 1, color=BAND_STYLE[b][0]) for b in BAND_STYLE]
    ax.legend(handles, [BAND_STYLE[b][2] for b in BAND_STYLE], loc="upper center", bbox_to_anchor=(0.42, -0.16),
              ncol=4, fontsize=8.5, handlelength=1.2, columnspacing=1.4)
    ax.set_title("One rule set decides every amount, and an unknown payee always needs her son")
    finish(ax, "8-decision-map.png",
           "Outcome by amount, computed to the sen by the real rules engine (rules.py). Numbers are risk scores.",
           note="TNB: median past bill RM 38.40; above 1.2× (RM 46.08) warns, above 1.5× (RM 57.60) goes to her son. "
                "Fewer than 3 past bills: RM 50 limit. Reproduce: python eval/run_decision_map.py",
           note_y=-0.3)


# ---- 9. Randomised attack test (Monte Carlo) ---------------------------------------------------
def fig_fuzz():
    data = load("invariant_fuzz_results.json")
    per = sorted(data["per_action"].items(), key=lambda kv: -kv[1]["count"])
    labels = [name for name, _ in per]
    segments = [
        ("Money moved", BLUE, [v.get("Money moved", 0) for _, v in per]),
        ("Refused by the server", ORANGE, [v.get("Refused", 0) for _, v in per]),
        ("Allowed, no money moved", AQUA, [v.get("Allowed, no money moved", 0) for _, v in per]),
    ]
    fig, ax = plt.subplots(figsize=(8, 4.6))
    left = [0] * len(labels)
    for name, color, values in segments:  # no numbers inside: only the payments count matters, shown at the end
        ax.barh(labels, values, left=left, color=color, height=0.55, edgecolor=SURFACE, linewidth=2, label=name)
        left = [a + b for a, b in zip(left, values)]
    for y, (_, v) in enumerate(per):
        paid = v.get("Money moved", 0)
        ax.text(v["count"] + 25, y, f"{v['count']:,}" + (f"  ·  {paid} paid" if paid else ""), va="center",
                fontsize=8.5, color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, max(v["count"] for _, v in per) * 1.22)
    ax.set_xlabel("random actions")
    ax.tick_params(axis="y", labelsize=9)
    ax.legend(loc="upper center", bbox_to_anchor=(0.35, -0.13), ncol=3, fontsize=9)
    broken = data["violation_count"]
    ax.set_title(f"{data['actions']:,} random actions, {broken} safety rules broken")
    finish(ax, "9-randomised-attack-test.png",
           f"{data['sequences']} random sequences of {data['steps_per_sequence']} actions on the real app; "
           f"{len(data['rules'])} safety rules checked after every action ({data['rule_checks']:,} checks).",
           note="Rules checked: never charged twice · blocked never paid · amount and payee frozen · balance adds up · "
                "review needs the son's right PIN · her payments safe or acknowledged only · never to the son's account · "
                "nothing after its deadline · finished stays finished · every payment logged · log only grows. "
                "Self-check: with the PIN check or the own-account check switched off, the same test reports violations. "
                f"Seed {data['seed']}; reproduce: python eval/run_invariant_fuzz.py",
           note_y=-0.25)


# ---- 10. Load curve ------------------------------------------------------------------------------
def fig_load():
    data = load("load_curve_results.json")
    lv = data["levels"]
    xs = [x["simultaneous"] for x in lv]
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    series = [
        ("Pay (confirm), median", BLUE, "-", "confirm_p50_ms"),
        ("Pay (confirm), 95th percentile", BLUE, (0, (4, 3)), "confirm_p95_ms"),
        ("New bill check, median", ORANGE, "-", "analyze_p50_ms"),
        ("New bill check, 95th percentile", ORANGE, (0, (4, 3)), "analyze_p95_ms"),
    ]
    for name, color, style, key in series:
        ax.plot(xs, [x[key] for x in lv], color=color, linestyle=style, linewidth=2, marker="o", markersize=5,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=name)
    ax.set_xscale("log")
    ax.set_xticks(xs)
    ax.set_xticklabels([str(x) for x in xs])
    ax.minorticks_off()
    ax.set_xlabel("simultaneous requests")
    ax.set_ylabel("response time (ms)")
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.legend(loc="upper center", bbox_to_anchor=(0.45, -0.2), ncol=2, fontsize=8.5, handlelength=3.2)
    rounds = [c for x in lv for c in x["charges_per_round"]]
    one_charge = sum(1 for c in rounds if c == 1)
    ax.set_title("More simultaneous requests: slower answers, still exactly one charge")
    cpus = re.search(r"(\d+) logical CPUs", data["machine"])
    finish(ax, "10-load-curve.png", f"Exactly 1 charge in {one_charge} of {len(rounds)} rounds; 0 server errors. "
           f"Real server on a laptop ({cpus.group(1) if cpus else '?'} logical CPUs): one process, one SQLite file.",
           note="Each level fires N identical 'pay' taps on one bill, and N new bill checks through the full rules path, "
                f"{lv[0]['rounds']} rounds per level. Duplicate taps are refused quickly, so the pay median stays low. "
                "A production deployment would use the bank's database. Reproduce: python eval/run_load_curve.py",
           note_y=-0.42)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for make in (fig_scam, fig_photos, fig_stress, fig_tests, fig_speech, fig_latency,
                 fig_decision_map, fig_fuzz, fig_load):
        try:
            make()
        except FileNotFoundError as missing:
            print("skipped", make.__name__, "- run its evaluation first:", missing.filename)
