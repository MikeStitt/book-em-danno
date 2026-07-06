"""Render a `danno bench` run into a human report: `report.md` + a self-contained,
publishable `report.html`.

The reporter is pure post-processing over `bench.json` (whose rows now carry the
`wire`/`resource`/`sidecars` sub-objects written by `suites/bench.py`) plus the
run-level `provenance.json`. Two shapes:

* **single-run** (`render_markdown`/`render_html`) — one section per permutation with
  the token split, tokens/sec, context-growth curve (§6.2), per-request RTT profile
  (§2.3), resource peaks (§5), and links to the raw sidecars (captures/transcripts).
* **merge** (`merge_markdown`/`merge_html`) — the cross-agent comparison grid that was
  `scratch/bench_merge.py`, kept so 4-agent runs still diff on one page.

`report.html` is intentionally self-contained (inline CSS, no external assets) so it
doubles as the published Artifact summary — a utilitarian data-dashboard treatment.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

# --- small formatting helpers -------------------------------------------------


def _fmt_int(value: object) -> str:
    """Thousands-separated int, or `—` when the field is absent."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "—"
    return f"{int(value):,}"


def _fmt_num(value: object, *, suffix: str = "") -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "—"
    return f"{value:g}{suffix}"


def _perm_label(row: dict) -> str:
    """`<suite> · <task>` — the permutation's readable identity."""
    return f"{row.get('suite', '?')} · {row.get('task', '?')}"


def _verdict_text(row: dict) -> str:
    """The verdict as a compact word — `bench.json` stores the enum's `str()`
    (`FailureClass.STALL`); strip the enum prefix and lowercase for display."""
    raw = str(row.get("verdict", "fail"))
    return raw.rsplit(".", 1)[-1].lower()


def _model_label(row: dict) -> str:
    model = row.get("model")
    if not model:
        return "claude (ref)"
    return str(model).removeprefix("ollama/")


def _tok_split(wire: dict | None) -> str:
    """`in→out (cached)` for the summary column."""
    if not wire:
        return "—"
    cached = wire.get("cached_tokens") or 0
    base = f"{_fmt_int(wire.get('input_tokens'))}→{_fmt_int(wire.get('output_tokens'))}"
    return f"{base} ({_fmt_int(cached)}c)" if cached else base


def _ctx_cell(wire: dict | None) -> str:
    """`peak-ctx (headroom%)` for the summary column."""
    if not wire or wire.get("peak_ctx_tokens") is None:
        return "—"
    peak = _fmt_int(wire.get("peak_ctx_tokens"))
    head = wire.get("ctx_headroom_pct")
    return f"{peak} ({head:g}% free)" if isinstance(head, int | float) else peak


# --- single-run markdown ------------------------------------------------------

_SUMMARY_COLS = (
    "permutation",
    "model",
    "result",
    "in→out (cache)",
    "tok/s",
    "ttft",
    "peak ctx",
    "cpu%",
    "vram(mb)",
    "latency",
)


def _summary_md_row(row: dict) -> str:
    wire = row.get("wire")
    res = row.get("resource")
    mark = "✓ pass" if row.get("passed") else f"✗ {_verdict_text(row)}"
    cells = [
        _perm_label(row),
        _model_label(row),
        mark,
        _tok_split(wire),
        _fmt_num(wire and wire.get("tok_per_s")),
        _fmt_num(wire and wire.get("ttft_s"), suffix="s"),
        _ctx_cell(wire),
        _fmt_num(res and res.get("cpu_peak")),
        _fmt_num(res and res.get("vram_peak_mb")),
        _fmt_num(row.get("latency_s"), suffix="s"),
    ]
    return "| " + " | ".join(cells) + " |"


def render_markdown(payload: dict, provenance: dict | None = None) -> str:
    rows = payload.get("results", [])
    passed = sum(1 for r in rows if r.get("passed"))
    lines: list[str] = [
        f"# danno bench — {payload.get('agent', '?')}",
        "",
        f"- generated: {payload.get('generated_at', '?')}",
        f"- models: {', '.join(payload.get('models') or []) or '—'}",
        f"- **{passed}/{len(rows)} passed**",
    ]
    if provenance:
        lines += _provenance_md(provenance)
    lines += [
        "",
        "## Summary",
        "",
        "| " + " | ".join(_SUMMARY_COLS) + " |",
        "|" + "---|" * len(_SUMMARY_COLS),
    ]
    lines += [_summary_md_row(r) for r in rows]
    lines += ["", "## Detail", ""]
    for r in rows:
        lines += _detail_md(r)
    return "\n".join(lines).rstrip() + "\n"


def _provenance_md(prov: dict) -> list[str]:
    host = prov.get("host") or {}
    danno = prov.get("danno") or {}
    parts: list[str] = []
    if host.get("cpu_model"):
        parts.append(f"- host: {host['cpu_model']} ({host.get('cpu_cores', '?')} cores)")
    for gpu in host.get("gpus") or []:
        parts.append(
            f"  - gpu: {gpu.get('name')} · driver {gpu.get('driver')} · "
            f"{_fmt_int(gpu.get('vram_total_mb'))}MB"
        )
    if danno.get("version") or danno.get("commit"):
        parts.append(f"- danno: {danno.get('version') or '?'} ({danno.get('commit') or '?'})")
    if prov.get("sample_interval_s") is not None:
        parts.append(f"- sampler interval: {prov['sample_interval_s']}s")
    return parts


def _detail_md(row: dict) -> list[str]:
    wire = row.get("wire")
    res = row.get("resource")
    sidecars = row.get("sidecars") or {}
    mark = "✓ pass" if row.get("passed") else f"✗ {_verdict_text(row)}"
    out = [f"### {_perm_label(row)} — {_model_label(row)}  ({mark})", ""]
    if wire:
        out.append(
            f"- tokens: in {_fmt_int(wire.get('input_tokens'))} · "
            f"out {_fmt_int(wire.get('output_tokens'))} · "
            f"cached {_fmt_int(wire.get('cached_tokens'))} · "
            f"{_fmt_num(wire.get('tok_per_s'))} tok/s over {wire.get('request_count', 0)} calls"
        )
        label = wire.get("ttft_label", "")
        out.append(f"- ttft: {_fmt_num(wire.get('ttft_s'), suffix='s')} ({label})")
        out.append(
            f"- rtt: min {_fmt_num(wire.get('rtt_min_s'), suffix='s')} · "
            f"mean {_fmt_num(wire.get('rtt_mean_s'), suffix='s')} · "
            f"max {_fmt_num(wire.get('rtt_max_s'), suffix='s')}"
        )
        growth = wire.get("ctx_growth") or []
        if growth:
            out.append(f"- context growth: {' → '.join(_fmt_int(g) for g in growth)}")
        deltas = wire.get("ctx_deltas") or []
        if deltas:
            out.append(f"- context deltas: {', '.join(f'+{_fmt_int(d)}' for d in deltas)}")
        out.append(f"- peak ctx: {_ctx_cell(wire)}")
    if res:
        out.append(
            f"- resources: cpu peak {_fmt_num(res.get('cpu_peak'))}% "
            f"(mean {_fmt_num(res.get('cpu_mean'))}%) · "
            f"gpu peak {_fmt_num(res.get('gpu_util_peak'))}% · "
            f"vram peak {_fmt_num(res.get('vram_peak_mb'))}MB · "
            f"mem peak {_fmt_int(res.get('mem_peak_kb'))}KB"
        )
        if res.get("model_load_s") is not None:
            out.append(f"- model load: {_fmt_num(res.get('model_load_s'), suffix='s')}")
    links = [f"[{name}]({path})" for name, path in _sidecar_links(sidecars)]
    if links:
        out.append(f"- artifacts: {' · '.join(links)}")
    if row.get("error"):
        out.append(f"- error: {row['error']}")
    out.append("")
    return out


def _sidecar_links(sidecars: dict) -> list[tuple[str, str]]:
    """`(label, relative-path)` pairs for a row's sidecar artifacts."""
    links: list[tuple[str, str]] = []
    if sidecars.get("transcript"):
        links.append(("transcript", sidecars["transcript"]))
    if sidecars.get("metrics"):
        links.append(("metrics", sidecars["metrics"]))
    for cap in sidecars.get("captures") or []:
        links.append(("capture", cap))
    if sidecars.get("samples"):
        links.append(("samples", sidecars["samples"]))
    return links


# --- single-run HTML (self-contained; doubles as the Artifact) ----------------

_CSS = """
:root{
  --ink:#14181f; --muted:#5b6672; --line:#dfe4ea; --panel:#ffffff;
  --ground:#f4f6f8; --accent:#0e7c86; --accent-soft:#e2f1f2;
  --good:#0f7b3f; --good-bg:#e5f4ea; --bad:#b3261e; --bad-bg:#fbe9e7; --warn:#8a5a00;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1140px;margin:0 auto;padding:32px 24px 64px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  margin:36px 0 12px;font-weight:600}
.meta{color:var(--muted);font-size:13px;margin:0 0 4px}
.tally{font-weight:700;color:var(--ink)}
.tnum{font-variant-numeric:tabular-nums}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:9px 12px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{position:sticky;top:0;background:var(--ground);color:var(--muted);font-weight:600;
  text-transform:uppercase;letter-spacing:.04em;font-size:11px;text-align:right}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--accent-soft)}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600}
.pill.pass{background:var(--good-bg);color:var(--good)}
.pill.fail{background:var(--bad-bg);color:var(--bad)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin:0 0 14px}
.card h3{margin:0 0 10px;font-size:15px;display:flex;gap:10px;align-items:center}
.card h3 .who{color:var(--muted);font-weight:500}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:12px 20px;margin:6px 0 12px}
.stat{display:flex;flex-direction:column;gap:1px}
.stat .k{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.stat .v{font-size:16px;font-variant-numeric:tabular-nums}
.spark{margin:8px 0 4px}
.links a{color:var(--accent);text-decoration:none;font-size:13px;margin-right:14px}
.links a:hover{text-decoration:underline}
.err{color:var(--bad);font-size:13px;margin-top:6px}
.foot{color:var(--muted);font-size:12px;margin-top:40px}
"""


def _pill(passed: bool, verdict: str) -> str:
    if passed:
        return '<span class="pill pass">pass</span>'
    return f'<span class="pill fail">{html.escape(verdict)}</span>'


def _sparkline(values: list[int], *, width: int = 220, height: int = 40) -> str:
    """A tiny inline-SVG polyline of the context-growth series (§6.2) with an
    emphasized endpoint. Empty/degenerate series render nothing."""
    pts = [v for v in values if isinstance(v, int | float)]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = hi - lo or 1
    n = len(pts) - 1
    coords = [
        (round(i / n * (width - 6) + 3, 1), round(height - 3 - (v - lo) / span * (height - 6), 1))
        for i, v in enumerate(pts)
    ]
    line = " ".join(f"{x},{y}" for x, y in coords)
    area = f"3,{height - 3} " + line + f" {width - 3},{height - 3}"
    ex, ey = coords[-1]
    return (
        f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="context growth">'
        f'<polyline points="{area}" fill="var(--accent-soft)" stroke="none"/>'
        f'<polyline points="{line}" fill="none" stroke="var(--accent)" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{ex}" cy="{ey}" r="3" fill="var(--accent)"/></svg>'
    )


def _stat(key: str, value: str) -> str:
    return (
        f'<div class="stat"><span class="k">{html.escape(key)}</span>'
        f'<span class="v">{value}</span></div>'
    )


def _summary_html_row(row: dict) -> str:
    wire = row.get("wire")
    res = row.get("resource")
    cells = [
        f"<td>{html.escape(_perm_label(row))}</td>",
        f"<td>{html.escape(_model_label(row))}</td>",
        f"<td>{_pill(bool(row.get('passed')), _verdict_text(row))}</td>",
        f'<td class="tnum">{html.escape(_tok_split(wire))}</td>',
        f'<td class="tnum">{html.escape(_fmt_num(wire and wire.get("tok_per_s")))}</td>',
        f'<td class="tnum">{html.escape(_fmt_num(wire and wire.get("ttft_s"), suffix="s"))}</td>',
        f'<td class="tnum">{html.escape(_ctx_cell(wire))}</td>',
        f'<td class="tnum">{html.escape(_fmt_num(res and res.get("cpu_peak")))}</td>',
        f'<td class="tnum">{html.escape(_fmt_num(res and res.get("vram_peak_mb")))}</td>',
        f'<td class="tnum">{html.escape(_fmt_num(row.get("latency_s"), suffix="s"))}</td>',
    ]
    return "<tr>" + "".join(cells) + "</tr>"


def _detail_html(row: dict) -> str:
    wire = row.get("wire")
    res = row.get("resource")
    sidecars = row.get("sidecars") or {}
    head = (
        f"<h3>{html.escape(_perm_label(row))} "
        f'<span class="who">{html.escape(_model_label(row))}</span> '
        f"{_pill(bool(row.get('passed')), _verdict_text(row))}</h3>"
    )
    stats: list[str] = []
    spark = ""
    if wire:
        stats += [
            _stat("input tok", _fmt_int(wire.get("input_tokens"))),
            _stat("output tok", _fmt_int(wire.get("output_tokens"))),
            _stat("cached tok", _fmt_int(wire.get("cached_tokens"))),
            _stat("tok/s", _fmt_num(wire.get("tok_per_s"))),
            _stat("ttft", _fmt_num(wire.get("ttft_s"), suffix="s")),
            _stat("rtt mean", _fmt_num(wire.get("rtt_mean_s"), suffix="s")),
            _stat("peak ctx", _ctx_cell(wire)),
            _stat("calls", _fmt_int(wire.get("request_count"))),
        ]
        spark = _sparkline(wire.get("ctx_growth") or [])
    if res:
        stats += [
            _stat("cpu peak", _fmt_num(res.get("cpu_peak"), suffix="%")),
            _stat("gpu peak", _fmt_num(res.get("gpu_util_peak"), suffix="%")),
            _stat("vram peak", _fmt_num(res.get("vram_peak_mb"), suffix="MB")),
            _stat("model load", _fmt_num(res.get("model_load_s"), suffix="s")),
        ]
    stats.append(_stat("latency", _fmt_num(row.get("latency_s"), suffix="s")))
    links = "".join(
        f'<a href="{html.escape(path)}">{html.escape(name)}</a>'
        for name, path in _sidecar_links(sidecars)
    )
    parts = [f'<div class="card">{head}']
    if spark:
        parts.append(f"<div>{spark}</div>")
    parts.append(f'<div class="grid">{"".join(stats)}</div>')
    if links:
        parts.append(f'<div class="links">{links}</div>')
    if row.get("error"):
        parts.append(f'<div class="err">{html.escape(str(row["error"]))}</div>')
    parts.append("</div>")
    return "".join(parts)


def render_html(payload: dict, provenance: dict | None = None) -> str:
    rows = payload.get("results", [])
    passed = sum(1 for r in rows if r.get("passed"))
    agent = html.escape(str(payload.get("agent", "?")))
    header = "".join(f"<th>{html.escape(c)}</th>" for c in _SUMMARY_COLS)
    summary = "".join(_summary_html_row(r) for r in rows)
    detail = "".join(_detail_html(r) for r in rows)
    meta = [
        f'<p class="meta">generated {html.escape(str(payload.get("generated_at", "?")))} · '
        f"models: {html.escape(', '.join(payload.get('models') or []) or '—')}</p>",
        f'<p class="meta tally">{passed}/{len(rows)} passed</p>',
    ]
    if provenance:
        meta += [
            f'<p class="meta">{html.escape(line.lstrip("- "))}</p>'
            for line in _provenance_md(provenance)
        ]
    return (
        f"<style>{_CSS}</style>"
        f'<div class="wrap">'
        f"<h1>danno bench · {agent}</h1>"
        f"{''.join(meta)}"
        f"<h2>Summary</h2>"
        f'<div class="tablewrap"><table><thead><tr>{header}</tr></thead>'
        f"<tbody>{summary}</tbody></table></div>"
        f"<h2>Per-permutation detail</h2>{detail}"
        f'<p class="foot">Generated by danno bench. Token, context, and latency figures are '
        f"derived from the redacted wire capture; resource peaks from the host sampler.</p>"
        f"</div>"
    )


def write_report(
    out_dir: Path, payload: dict, *, provenance: dict | None = None
) -> tuple[Path, Path]:
    """Write `report.md` + `report.html` into `out_dir`; return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "report.md"
    html_path = out_dir / "report.html"
    md_path.write_text(render_markdown(payload, provenance), encoding="utf-8")
    html_path.write_text(render_html(payload, provenance), encoding="utf-8")
    return md_path, html_path


# --- multi-agent merge (promoted from scratch/bench_merge.py) ------------------


def _col_label(payload: dict) -> str:
    agent = payload.get("agent", "?")
    models = payload.get("models") or []
    if agent == "claude":
        return "claude (ref)"
    model = models[0] if len(models) == 1 else ",".join(models) or "?"
    model = model.removeprefix("ollama/").removesuffix(":latest")
    return f"{agent}\n{model}" if model else agent


def _merge_cell(row: dict | None) -> str:
    if row is None:
        return "—"
    mark = "✓" if row.get("passed") else "✗"
    parts = [mark]
    wire = row.get("wire") or {}
    if row.get("tool_calls") is not None:
        parts.append(f"{row['tool_calls']}tc")
    if wire.get("tok_per_s") is not None:
        parts.append(f"{wire['tok_per_s']:g}t/s")
    if row.get("latency_s") is not None:
        parts.append(f"{row['latency_s']:g}s")
    return " · ".join(parts)


def load(paths: list[Path]) -> list[dict]:
    out = []
    for p in paths:
        payload = json.loads(p.read_text(encoding="utf-8"))
        payload["_source"] = str(p)
        out.append(payload)
    return out


def _merge_grid(payloads: list[dict]) -> tuple[list[str], list[str], dict]:
    cols = [_col_label(p) for p in payloads]
    tasks: list[str] = []
    grid: dict[tuple[int, str], dict] = {}
    for ci, p in enumerate(payloads):
        for r in p.get("results", []):
            task = f"{r.get('suite', '?')}/{r.get('task', '?')}"
            if task not in tasks:
                tasks.append(task)
            grid[(ci, task)] = r
    return cols, tasks, grid


def merge_markdown(payloads: list[dict]) -> str:
    cols, tasks, grid = _merge_grid(payloads)
    flat = [c.replace("\n", " ") for c in cols]
    lines = ["| task | " + " | ".join(flat) + " |", "|" + "---|" * (len(flat) + 1)]
    for task in tasks:
        cells = [_merge_cell(grid.get((ci, task))) for ci in range(len(cols))]
        lines.append(f"| {task} | " + " | ".join(cells) + " |")
    tally = []
    for ci in range(len(cols)):
        present = [r for t in tasks if (r := grid.get((ci, t)))]
        passed = sum(1 for r in present if r.get("passed"))
        tally.append(f"**{passed}/{len(present)}**" if present else "—")
    lines.append("| **passed** | " + " | ".join(tally) + " |")
    return "\n".join(lines) + "\n"


def merge_html(payloads: list[dict]) -> str:
    cols, tasks, grid = _merge_grid(payloads)

    def th(text: str) -> str:
        return "<th>" + html.escape(text).replace("\n", "<br>") + "</th>"

    head = "<tr><th>task</th>" + "".join(th(c) for c in cols) + "</tr>"
    body = []
    for task in tasks:
        tds = []
        for ci in range(len(cols)):
            r = grid.get((ci, task))
            cls = "" if r is None else ("pass" if r.get("passed") else "fail")
            tds.append(f'<td class="{cls} tnum">{html.escape(_merge_cell(r))}</td>')
        body.append(f"<tr><td>{html.escape(task)}</td>" + "".join(tds) + "</tr>")
    tally = []
    for ci in range(len(cols)):
        rows = [r for t in tasks if (r := grid.get((ci, t)))]
        passed = sum(1 for r in rows if r.get("passed"))
        tally.append(f"<td><b>{passed}/{len(rows)}</b></td>" if rows else "<td>—</td>")
    body.append("<tr><td><b>passed</b></td>" + "".join(tally) + "</tr>")
    return (
        f"<style>{_CSS}"
        "td.pass{background:var(--good-bg)}td.fail{background:var(--bad-bg)}</style>"
        f'<div class="wrap"><h1>danno bench · comparison</h1>'
        f'<div class="tablewrap"><table><thead>{head}</thead><tbody>{"".join(body)}</tbody>'
        "</table></div></div>"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: merge several `bench.json` files into one comparison (markdown + optional HTML)."""
    ap = argparse.ArgumentParser(description="Merge danno bench.json files into a comparison.")
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--html", type=Path)
    args = ap.parse_args(argv)
    payloads = load(args.paths)
    print(merge_markdown(payloads))
    if args.html:
        args.html.write_text(merge_html(payloads), encoding="utf-8")
        print(f"wrote {args.html}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
