// Build LLM_TRADING_MODEL_OVERVIEW.docx
// Comprehensive implementation overview for the LLM trading model fork.
//
// Usage:
//   cd "C:\trading\LLM model"
//   npm install docx                                      # one-time, local node_modules
//   node scripts/build_overview_doc.js docs/LLM_TRADING_MODEL_OVERVIEW.docx
//
// The output .docx is gitignored (see .gitignore root). Regenerate after
// architectural changes (new phases complete, schema split landing,
// hardware swap, etc.) so the overview stays in sync with the markdown
// design specs in docs/.
//
// Validation (optional, requires the docx skill scripts on the host):
//   python scripts/office/validate.py docs/LLM_TRADING_MODEL_OVERVIEW.docx

const fs = require('fs');
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, LevelFormat, HeadingLevel,
  BorderStyle, WidthType, ShadingType, PageNumber, PageBreak,
  TabStopType, TabStopPosition,
} = require('docx');

// ----------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------

const border = { style: BorderStyle.SINGLE, size: 6, color: "999999" };
const borders = { top: border, bottom: border, left: border, right: border };
const lightBorder = { style: BorderStyle.SINGLE, size: 4, color: "DDDDDD" };
const lightBorders = { top: lightBorder, bottom: lightBorder, left: lightBorder, right: lightBorder };

const CONTENT_WIDTH = 9360; // 12240 - 1440*2 = 9360 DXA (US Letter, 1" margins)

function p(text, opts = {}) {
  return new Paragraph({
    spacing: { after: 120, ...opts.spacing },
    alignment: opts.alignment,
    children: [new TextRun({ text, ...opts.run })],
  });
}

function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 360, after: 180 },
    children: [new TextRun({ text, bold: true, size: 32, font: "Arial" })],
  });
}

function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 240, after: 120 },
    children: [new TextRun({ text, bold: true, size: 26, font: "Arial" })],
  });
}

function h3(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    spacing: { before: 200, after: 100 },
    children: [new TextRun({ text, bold: true, size: 22, font: "Arial" })],
  });
}

function bullet(text, level = 0) {
  return new Paragraph({
    numbering: { reference: "bullets", level },
    spacing: { after: 80 },
    children: [new TextRun({ text })],
  });
}

function bulletRich(runs, level = 0) {
  return new Paragraph({
    numbering: { reference: "bullets", level },
    spacing: { after: 80 },
    children: runs,
  });
}

function numbered(text) {
  return new Paragraph({
    numbering: { reference: "numbers", level: 0 },
    spacing: { after: 80 },
    children: [new TextRun({ text })],
  });
}

function code(text) {
  return new Paragraph({
    spacing: { after: 80 },
    children: [new TextRun({ text, font: "Consolas", size: 20 })],
  });
}

function pageBreak() {
  return new Paragraph({ children: [new PageBreak()] });
}

// Make a 2-column table from an array of [label, value] pairs.
function makeKV(rows, labelWidth = 3000) {
  const valueWidth = CONTENT_WIDTH - labelWidth;
  return new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [labelWidth, valueWidth],
    rows: rows.map(([k, v]) => new TableRow({
      children: [
        new TableCell({
          borders: lightBorders,
          width: { size: labelWidth, type: WidthType.DXA },
          margins: { top: 80, bottom: 80, left: 120, right: 120 },
          shading: { fill: "F5F5F5", type: ShadingType.CLEAR },
          children: [new Paragraph({ children: [new TextRun({ text: k, bold: true })] })],
        }),
        new TableCell({
          borders: lightBorders,
          width: { size: valueWidth, type: WidthType.DXA },
          margins: { top: 80, bottom: 80, left: 120, right: 120 },
          children: [new Paragraph({ children: [new TextRun({ text: v })] })],
        }),
      ],
    })),
  });
}

// Make a generic table with header row + body rows, equal column widths.
function makeTable(headers, rows) {
  const colCount = headers.length;
  const colWidth = Math.floor(CONTENT_WIDTH / colCount);
  const widths = Array(colCount).fill(colWidth);
  // adjust last to absorb rounding
  widths[colCount - 1] = CONTENT_WIDTH - colWidth * (colCount - 1);

  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map((h, i) => new TableCell({
      borders,
      width: { size: widths[i], type: WidthType.DXA },
      margins: { top: 100, bottom: 100, left: 120, right: 120 },
      shading: { fill: "2E75B6", type: ShadingType.CLEAR },
      children: [new Paragraph({
        children: [new TextRun({ text: h, bold: true, color: "FFFFFF" })],
      })],
    })),
  });

  const bodyRows = rows.map(row => new TableRow({
    children: row.map((cell, i) => new TableCell({
      borders,
      width: { size: widths[i], type: WidthType.DXA },
      margins: { top: 80, bottom: 80, left: 120, right: 120 },
      children: [new Paragraph({ children: [new TextRun({ text: cell })] })],
    })),
  }));

  return new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: widths,
    rows: [headerRow, ...bodyRows],
  });
}

// Make a table with custom column widths
function makeTableCustom(headers, rows, widths) {
  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map((h, i) => new TableCell({
      borders,
      width: { size: widths[i], type: WidthType.DXA },
      margins: { top: 100, bottom: 100, left: 120, right: 120 },
      shading: { fill: "2E75B6", type: ShadingType.CLEAR },
      children: [new Paragraph({
        children: [new TextRun({ text: h, bold: true, color: "FFFFFF" })],
      })],
    })),
  });

  const bodyRows = rows.map(row => new TableRow({
    children: row.map((cell, i) => new TableCell({
      borders,
      width: { size: widths[i], type: WidthType.DXA },
      margins: { top: 80, bottom: 80, left: 120, right: 120 },
      children: [new Paragraph({ children: [new TextRun({ text: cell, size: 20 })] })],
    })),
  }));

  return new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: widths,
    rows: [headerRow, ...bodyRows],
  });
}

// ----------------------------------------------------------------------
// Document content
// ----------------------------------------------------------------------

const children = [];

// ===== Title block =====
children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { before: 600, after: 120 },
  children: [new TextRun({ text: "LLM Trading Model", bold: true, size: 48, font: "Arial" })],
}));
children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 120 },
  children: [new TextRun({ text: "Implementation Overview", size: 32, font: "Arial", color: "555555" })],
}));
children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 360 },
  children: [new TextRun({
    text: "Tiered LLM signal generation for intraday equity trading",
    italics: true, size: 22, color: "666666",
  })],
}));
children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 600 },
  children: [
    new TextRun({ text: "Repository: trading-model-llm  |  Version: Phase B complete", size: 20 }),
  ],
}));
children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 600 },
  children: [
    new TextRun({ text: "Document date: 2026-05-09", size: 20 }),
  ],
}));

children.push(pageBreak());

// ===== 1. Executive Summary =====
children.push(h1("1. Executive Summary"));
children.push(p(
  "This document describes the LLM Trading Model: an intraday equity trading system whose signal generation is performed by large language models rather than fixed rule-based heuristics. It is one of two strategy forks in the broader trading platform; the sibling fork, trading-model-gap-and-go, runs deterministic gap-and-go logic against a Russell 2000 universe. Both share infrastructure (data feeds, broker, risk validation, deploy procedures) inherited from the trading-platform base repository."
));
children.push(p(
  "The signal generator uses a three-tier evaluation architecture. Tier 1 (Qwen 3.6-27B running locally on a workstation GPU) evaluates every candidate every cycle. Tier 2 (Claude Sonnet 4.5) is invoked only on ambiguous, high-stakes setups. Tier 3 (Claude Opus 4.6) labels decisions offline as a gold standard for evaluation. The design balances cost, latency, privacy, and decision quality across complementary models rather than depending on any single one."
));
children.push(p(
  "As of this document’s writing, the package scaffolding, schema, prompt template, escalation logic, merge logic, and orchestrator are complete and validated by 87 mocked-test assertions plus one live Anthropic Haiku call. The local Tier 1 backend is a placeholder pending workstation hardware delivery; a stand-in routes to Claude Haiku 4.5 in the bridge period so all downstream code paths can be exercised against real cloud inference."
));

// ===== 2. Goals & Success Criteria =====
children.push(h1("2. Goals & Success Criteria"));

children.push(h2("2.1 Why an LLM strategy"));
children.push(p(
  "Rule-based gap-and-go produces strong signals on liquid small/mid-cap setups but cannot reason about catalyst quality, regime drift, or novel patterns. An LLM trader can weigh news content against price action, distinguish a rumored partnership from a confirmed FDA approval, and adapt to regimes the rule library never saw at design time. The hypothesis is that an LLM’s holistic judgment outperforms or complements rule-based signal generation on a meaningful subset of setups."
));
children.push(p(
  "The platform already runs Claude Haiku 4.5 in production for headline sentiment scoring. The LLM trading model extends that role from “score this headline” to “decide what to do.”"
));

children.push(h2("2.2 Success criteria"));
children.push(p("The LLM model is considered worth deploying if it satisfies the following on a 30-90 day backtest:"));
children.push(bullet("Win rate at least comparable to the rule-based base on the same period"));
children.push(bullet("Net P&L per dollar at risk no worse than base"));
children.push(bullet("Maximum drawdown bounded; no catastrophic loss days"));
children.push(bullet("Confidence score correlates with realized P&L (calibration)"));
children.push(bullet("Decision quality holds through both trending and choppy regimes"));
children.push(bullet("Operational complexity is acceptable: clean fallbacks, deterministic replay, traceable failures"));

children.push(h2("2.3 Out of scope"));
children.push(bullet("Replacement of human discretion; this is paper-trading research, not a managed account"));
children.push(bullet("High-frequency execution; the system evaluates on 5-minute bars"));
children.push(bullet("Options, futures, or non-US equities"));
children.push(bullet("Position management beyond bracket stops; all positions flatten at 15:55 ET"));

// ===== 3. Strategy & Architecture =====
children.push(h1("3. Strategy & Architecture"));

children.push(h2("3.1 Tiered evaluation"));
children.push(p(
  "Three model tiers, each chosen for what it does best and bounded by what it costs:"
));

children.push(makeTableCustom(
  ["Tier", "Model", "Role", "Volume / day"],
  [
    ["1", "Qwen 3.6-27B (local)", "Hot path; every candidate every cycle", "30-200 × 78 cycles"],
    ["2", "Claude Sonnet 4.5", "Selective escalation on ambiguous setups", "5-15 (cap 25)"],
    ["3", "Claude Opus 4.6", "Offline gold-standard labeler (M2 replay + weekly audit)", "Replay-time only"],
  ],
  [800, 2400, 4760, 1400],
));

children.push(h2("3.2 Decision flow"));
children.push(p("Per (ticker, timestamp) candidate that passes pre-filtering:"));
children.push(numbered("Build LLMContext from production data: market context, ticker fundamentals, daily regime, intraday indicators, news, position state, decision history, time-of-day"));
children.push(numbered("Render the prompt: stable system prefix (cacheable), market-context block (cacheable per cycle), per-ticker block (variable)"));
children.push(numbered("Tier 1 always called. Returns a Pydantic-validated LLMDecision. Failure collapses to Hold(reason)"));
children.push(numbered("Escalation rule fires Tier 2 only if all four gates hold: confidence in [50, 75], high-quality catalyst flag present, pre-market RVOL > 3x, daily budget not exhausted"));
children.push(numbered("Merge: if T1 and T2 agree on action, take higher confidence and T2's reasoning; if they disagree, default to Hold"));
children.push(numbered("Live decision goes to the existing risk validator and bracket-order placer, which apply the same gates that the rule-based fork uses"));

children.push(h2("3.3 Why this structure"));
children.push(p("Three reasons the tiered design is strictly better than putting Claude in the hot path:"));
children.push(bulletRich([
  new TextRun({ text: "Privacy preserved. ", bold: true }),
  new TextRun({ text: "99%+ of decisions stay on-workstation. Only escalations leak strategy details to Anthropic." }),
]));
children.push(bulletRich([
  new TextRun({ text: "Determinism preserved. ", bold: true }),
  new TextRun({ text: "Tier 1 weights are immutable; same input produces same output across replays in 2027. Tier 2 versions can drift but are documented and pinned." }),
]));
children.push(bulletRich([
  new TextRun({ text: "Latency bounded. ", bold: true }),
  new TextRun({ text: "Tier 1 local adds 3-5s per candidate; Tier 2 adds another 1-2s but only on the 5-15 candidates per day where it fires. Net cycle time is unchanged." }),
]));
children.push(p("And three reasons it is strictly better than running Qwen alone:"));
children.push(bulletRich([
  new TextRun({ text: "Domain expertise on hard cases. ", bold: true }),
  new TextRun({ text: "Qwen 3.6-27B has documented finance-domain reasoning gaps versus Claude. The 5-15 escalations per day are exactly the catalyst-driven setups where the gap matters." }),
]));
children.push(bulletRich([
  new TextRun({ text: "Calibration anchor. ", bold: true }),
  new TextRun({ text: "Weekly Opus audits compare Qwen's recent decisions against a stronger reasoner; systematic biases surface as prompt-engineering signals." }),
]));
children.push(bulletRich([
  new TextRun({ text: "Diversity of error. ", bold: true }),
  new TextRun({ text: "When T1 and T2 agree, confidence is better justified. When they disagree, Hold is the safe default - disagreement equals no edge." }),
]));

children.push(pageBreak());

// ===== 4. Tools & Stack =====
children.push(h1("4. Tools & Technology Stack"));

children.push(h2("4.1 Inference"));
children.push(makeTableCustom(
  ["Component", "Provider", "Purpose"],
  [
    ["Anthropic SDK (anthropic>=0.45)", "Anthropic", "Tier 2 Sonnet, Tier 3 Opus, Tier-1 stand-in (Haiku)"],
    ["OpenAI SDK (openai>=1.50)", "LM Studio (workstation)", "Tier 1 Qwen 3.6-27B local; LM Studio exposes OpenAI-compatible API"],
    ["LM Studio", "Local app", "Hosts and serves the local Qwen 3.6-27B model on RTX PRO 5000"],
  ],
  [3120, 3120, 3120],
));

children.push(h2("4.2 Schema, validation, retry"));
children.push(makeTableCustom(
  ["Library", "Version", "Purpose"],
  [
    ["pydantic", ">=2.5", "LLMDecision schema validation + JSON schema generation for tool-use"],
    ["tenacity", ">=8.2", "Exponential-backoff retry on transient API errors (3 attempts)"],
    ["pyyaml", ">=6.0", "settings.yaml parsing"],
    ["pytest, pytest-asyncio", ">=8.2, >=0.23", "Test framework with async support"],
  ],
  [2200, 1400, 5760],
));

children.push(h2("4.3 Data, broker, news, sentiment"));
children.push(p(
  "These are inherited unchanged from the trading-platform base codebase. The LLM model fork does not reimplement them."
));
children.push(makeTableCustom(
  ["Service", "Subscription", "Cost", "Role"],
  [
    ["Alpaca paper", "Free", "$0", "Broker, account state"],
    ["Alpaca SIP (Algo Trader Plus)", "Paid", "$99/mo", "Real-time consolidated bars"],
    ["Polygon Stocks Starter", "Paid", "$29/mo", "Historical bars, news"],
    ["Alpaca News (Benzinga)", "Free with broker", "$0", "Real-time news WebSocket"],
    ["Anthropic API", "Pay-per-use", "~$2-5/day", "Sentiment scoring + LLM signal generation"],
    ["Finnhub", "Free tier", "$0", "Earnings calendar veto"],
    ["Hetzner CPX21 VPS", "Paid", "$8/mo", "Production trader host"],
  ],
  [3300, 1900, 1300, 2860],
));

// ===== 5. Infrastructure =====
children.push(h1("5. Infrastructure"));

children.push(h2("5.1 Workstation (LLM model home)"));
children.push(p(
  "A dedicated Puget workstation runs Tier 1 inference and any heavy backtest workloads. The workstation is the single most important piece of LLM-model infrastructure: its 48GB GPU is what enables 70B-class models to run locally at zero marginal token cost."
));
children.push(makeKV([
  ["Platform", "Puget Workstation Core Ultra Z890 C132-XL"],
  ["GPU", "NVIDIA RTX PRO 5000 Blackwell 48GB (centerpiece)"],
  ["CPU", "Intel Core Ultra 7 270K Plus 24-core 3.7 GHz"],
  ["RAM", "192 GB DDR5-4800 (4 x 48 GB Kingston Fury Renegade)"],
  ["Storage", "6 TB NVMe Gen4 (1 + 4 + 1 TB Samsung 990 Pro)"],
  ["PSU / Cooling", "Super Flower LEADEX Titanium 1700W; Asetek 240mm AIO"],
  ["OS", "Windows 11 Pro 64-bit"],
  ["Pre-installed", "LM Studio, NVIDIA App"],
], 2400));

children.push(h2("5.2 Production VPS"));
children.push(p(
  "The live trader runs on a Hetzner CPX21 instance in Ashburn, VA. Today the VPS hosts the gap-and-go fork (rule-based). The LLM model will run alongside in shadow mode, and may eventually graduate to placing live (paper) orders. The workstation hosts inference; the VPS hosts execution. They communicate over the network."
));
children.push(makeKV([
  ["Provider", "Hetzner CPX21 (Ashburn VA)"],
  ["Hostname", "trader-prod"],
  ["IP", "5.161.199.155"],
  ["Service unit", "trader.service (systemd)"],
  ["Service user", "trader:trader"],
  ["Working dir", "/opt/trader/app/"],
  ["Python venv", "/opt/trader/.venv/bin/python"],
  ["Env file", "/etc/trading-platform/env"],
  ["Hardening", "ProtectSystem=strict, ProtectHome=true, ReadWritePaths=/opt/trader/app"],
  ["Cost", "$8/mo"],
], 2400));

children.push(h2("5.3 Cost summary"));
children.push(makeTableCustom(
  ["Path", "Backend", "Volume", "Cost / day"],
  [
    ["Tier 1 (local)", "Qwen 3.6-27B local", "30-200 × 78 cycles", "~$0.20 (electricity)"],
    ["Tier 1 fallback", "Anthropic Haiku/Sonnet", "Only on workstation outage", "$5-20 during outage"],
    ["Tier 2 escalation", "Anthropic Sonnet", "5-15 / day (cap 25)", "~$0.10-0.30"],
    ["Tier 3 weekly audit", "Anthropic Opus", "~12K decisions per audit", "~$2-5 amortized"],
    ["Sentiment scoring", "Anthropic Haiku", "50-150 calls / day", "~$2-4"],
    ["Live operating total", "All", "Per trading day", "~$5-10"],
  ],
  [2200, 2400, 2700, 2060],
));

children.push(pageBreak());

// ===== 6. System Requirements =====
children.push(h1("6. System Requirements"));

children.push(h2("6.1 Software"));
children.push(bullet("Python 3.12+ (workstation runs 3.12 via the LM Studio host venv; dev laptop runs 3.14)"));
children.push(bullet("Node.js (only required for the docx skill used to produce overview documents)"));
children.push(bullet("git for version control and deployment"));
children.push(bullet("LM Studio on the workstation (pre-installed on the Puget build)"));
children.push(bullet("OpenSSH client for VPS deploys"));

children.push(h2("6.2 Hardware floor"));
children.push(bullet("Workstation: 48 GB VRAM minimum to run Qwen 3.6-27B at 4-bit. 32B variants run on lower-VRAM GPUs but trade decision quality for size"));
children.push(bullet("Workstation RAM: 64 GB sufficient; 192 GB lets the M2 replay harness keep the entire 1-min bar dataset in memory"));
children.push(bullet("VPS: 4 vCPU / 8 GB RAM is more than enough; CPX21 (3 vCPU, 4 GB) handles the existing trader plus the LLM client without issue"));
children.push(bullet("Network: outbound HTTPS (port 443) to api.anthropic.com, paper-api.alpaca.markets, api.polygon.io, finnhub.io"));

children.push(h2("6.3 Storage"));
children.push(bullet("Workstation: 6 TB NVMe across three drives (OS + code, market data archives + model weights, cache)"));
children.push(bullet("Single 70B 4-bit model weight file is approximately 40 GB; budget room for two or three variants"));
children.push(bullet("VPS: 75 GB total; deploy + database + journals use under 5 MB per day"));

children.push(h2("6.4 Required environment variables"));
children.push(p("Set in /etc/trading-platform/env on the VPS, or in the dev shell for local runs:"));
children.push(code("ALPACA_API_KEY"));
children.push(code("ALPACA_API_SECRET"));
children.push(code("ANTHROPIC_API_KEY"));
children.push(code("POLYGON_API_KEY"));
children.push(code("FINNHUB_API_KEY"));
children.push(code("DATABENTO_API_KEY  (optional; futures.enabled=false by default)"));

// ===== 7. Setup =====
children.push(h1("7. Setup Instructions"));

children.push(h2("7.1 Repository layout"));
children.push(p(
  "The LLM model fork is its own GitHub repository (NZ1979/trading-model-llm), forked from trading-platform at tag v0.9-pre-phase-c-deploy. The base remote is wired as upstream so improvements land in the base and propagate via git fetch upstream && git merge upstream/main."
));

children.push(h2("7.2 First-time clone (workstation or dev laptop)"));
children.push(code("cd C:\\trading"));
children.push(code("git clone git@github.com:NZ1979/trading-model-llm.git \"LLM model\""));
children.push(code("cd \"LLM model\""));
children.push(code("git remote add upstream git@github.com:NZ1979/trading-platform.git"));

children.push(h2("7.3 Python environment"));
children.push(code("python -m venv .venv"));
children.push(code(".venv\\Scripts\\Activate.ps1   # PowerShell on Windows"));
children.push(code("pip install -r requirements.txt -r requirements-llm.txt"));
children.push(p("requirements.txt holds the base dependencies (pandas, anthropic, websockets, etc.). requirements-llm.txt adds anthropic>=0.45 (for prompt caching), openai (LM Studio client), pydantic, tenacity, and pytest-asyncio."));

children.push(h2("7.4 Configuration"));
children.push(p(
  "config/settings.yaml ships with sensible defaults. The llm: block defaults to enabled: false so the live signal engine cannot start until explicitly flipped. When wiring the LLM path into main.py for shadow mode, set llm.enabled: true."
));
children.push(makeTableCustom(
  ["Key", "Default", "Meaning"],
  [
    ["llm.enabled", "false", "Master switch for the LLM signal engine"],
    ["llm.prompt_version", "v0.0-stub", "Bumps invalidate per-tier cache namespaces"],
    ["llm.t1.backend", "haiku_stand_in", "haiku_stand_in (bridge) | anthropic | qwen_local (workstation)"],
    ["llm.t1.model_id", "claude-haiku-4-5", "Pinned for haiku_stand_in"],
    ["llm.t2.enabled", "true", "Sonnet selective escalation"],
    ["llm.t2.max_per_day", "25", "Daily escalation budget cap"],
    ["llm.t2.confidence_floor", "50", "Lower edge of escalation band"],
    ["llm.t2.confidence_ceiling", "75", "Upper edge of escalation band"],
    ["llm.t2.pm_rvol_min", "3.0", "Pre-market RVOL gate"],
    ["llm.t3.enabled", "false", "Live default off; replay/audit jobs override per-run"],
  ],
  [3000, 1800, 4560],
));

children.push(h2("7.5 LM Studio (workstation only)"));
children.push(numbered("Open LM Studio (pre-installed on the Puget build)"));
children.push(numbered("Search and download Qwen 3.6-27B Instruct, 4-bit quantization"));
children.push(numbered("Load the model and verify VRAM utilization stays under 48 GB"));
children.push(numbered("Start the local server (default localhost:1234, OpenAI-compatible /v1)"));
children.push(numbered("In settings.yaml, change llm.t1.backend from haiku_stand_in to qwen_local and llm.t1.model_id to qwen3.6-27b-instruct-q4 (or whatever LM Studio reports)"));
children.push(numbered("Restart the trader. Tier 1 now hits the local model; Tier 2 and Tier 3 stay on Anthropic"));

children.push(h2("7.6 Verification"));
children.push(p("Three local verification scripts confirm the package is wired correctly without any real API calls:"));
children.push(code("$env:PYTHONPATH = \".\""));
children.push(code("python scripts/verify_anthropic_client.py     # 14 assertions: tool schema, retry, clamp/truncate"));
children.push(code("python scripts/verify_llm_factory.py          # 15 assertions: config-driven client construction"));
children.push(code("python scripts/verify_signal_engine.py        # 13 assertions: 13 code paths through evaluate()"));
children.push(code("python scripts/verify_prompts.py              # 33 assertions: prompt rendering + cache breakpoints"));
children.push(p("All four scripts print ALL OK on a healthy install. One additional optional script (smoke_test_haiku.py) makes two real Haiku API calls to validate end-to-end behavior; cost ~$0.007."));

children.push(pageBreak());

// ===== 8. Implementation Detail =====
children.push(h1("8. Implementation Detail"));

children.push(h2("8.1 Package layout"));
children.push(code("strategy/llm/"));
children.push(code("    __init__.py        # public exports: LLMContext, LLMDecision"));
children.push(code("    types.py           # LLMContext (frozen dataclass), LLMDecision (Pydantic)"));
children.push(code("    clients.py         # LLMClient protocol, AnthropicClient, LocalClient, typed errors"));
children.push(code("    prompts.py         # v1 templates + render_messages(ctx)"));
children.push(code("    escalation.py      # escalation_rule + EscalationBudget"));
children.push(code("    merge.py           # merge_tiers"));
children.push(code("    signal_engine.py   # TierClients, evaluate (orchestrator)"));
children.push(code("    factory.py         # build_tier_clients, build_escalation_budget from settings.yaml"));

children.push(h2("8.2 LLMContext"));
children.push(p(
  "Frozen dataclass with 38 fields covering meta, market context, ticker fundamentals, daily regime, intraday indicators, news/sentiment, position state, decision history, and time-of-day. All fields except ticker / timestamp_et / prompt_version have sensible defaults so partial contexts construct cleanly during tests. The same context object is fed to every tier; no tier ever sees another tier’s output."
));

children.push(h2("8.3 LLMDecision"));
children.push(p(
  "Pydantic model. The LLM returns this via the submit_decision tool; tool-use enforces required fields and enum/type. Pydantic enforces value bounds via permissive normalization: out-of-range numerics clamp to bounds, over-long strings truncate to max length minus three plus an ellipsis. Genuinely malformed input (wrong enum, missing required field) raises ValidationError, which the signal engine maps to Hold(reason=\"schema_invalid\")."
));
children.push(makeTableCustom(
  ["Field", "Type / Range", "Source"],
  [
    ["action", "Buy | Sell | Hold", "LLM"],
    ["confidence", "int [0, 100]", "LLM (clamped)"],
    ["setup_label", "string ≤ 50 chars", "LLM (truncated)"],
    ["reasoning", "string ≤ 280 chars", "LLM (truncated)"],
    ["stop_loss_atr_multiple", "float [1.0, 3.0]", "LLM (clamped)"],
    ["take_profit_atr_multiple", "float [1.0, 5.0]", "LLM (clamped)"],
    ["time_horizon", "intraday | overnight | multi_day", "LLM"],
    ["concerns", "list[str], cap 5", "LLM"],
    ["alternative_view", "string ≤ 140 chars", "LLM (truncated)"],
    ["tier_provenance", "enum (t1_only | t1_t2_agree | ... )", "Signal engine"],
    ["raw_response", "dict (token usage, cache headers)", "Signal engine"],
  ],
  [2900, 2500, 3960],
));

children.push(h2("8.4 Prompt template (v1.0)"));
children.push(p(
  "render_messages(ctx) returns a {system, messages} dict that unpacks directly into Anthropic’s messages.create(...). Three blocks, two cache breakpoints:"
));
children.push(bulletRich([
  new TextRun({ text: "System block ", bold: true }),
  new TextRun({ text: "(cache_control: ephemeral): role + decision criteria + tool reminder. Stable across cycles within a prompt_version." }),
]));
children.push(bulletRich([
  new TextRun({ text: "Market-context user block ", bold: true }),
  new TextRun({ text: "(cache_control: ephemeral): SPY change, VIX, regime label. Cycle-stable; refreshes when the SPY 5-min bar updates." }),
]));
children.push(bulletRich([
  new TextRun({ text: "Per-ticker user block ", bold: true }),
  new TextRun({ text: "(no cache_control): ticker fundamentals, daily regime, intraday bars, news, position, history, time-of-day. Per-call variable." }),
]));
children.push(p(
  "Note: at the v1.0 prompt size (~265 cacheable tokens), Anthropic’s prompt cache does not engage on Haiku because the prefix is below the ~2048-token minimum. v1.1 will fold few-shot examples into the system block to push the prefix above the threshold and unlock cache savings."
));

children.push(h2("8.5 Escalation rule"));
children.push(p("Tier 2 fires only when ALL four gates hold:"));
children.push(numbered("Daily escalation budget has remaining capacity (default cap: 25/day)"));
children.push(numbered("Tier 1 confidence is in the uncertain middle [50, 75]. Above 75: T1 already confident. Below 50: weak signal becomes Hold anyway."));
children.push(numbered("Candidate has at least one high-quality catalyst flag set: FDA_approval, M&A, earnings_beat_with_guidance_raise, breakthrough_news"));
children.push(numbered("Pre-market RVOL exceeds 3.0x"));

children.push(h2("8.6 Merge logic"));
children.push(bulletRich([
  new TextRun({ text: "Both tiers agree on action: ", bold: true }),
  new TextRun({ text: "take the higher confidence, use Tier 2's reasoning, tag tier_provenance=t1_t2_agree" }),
]));
children.push(bulletRich([
  new TextRun({ text: "Tiers disagree: ", bold: true }),
  new TextRun({ text: "synthesize a Hold with confidence=0 and tag t1_t2_disagree. Disagreement equals no edge; do not trade." }),
]));

children.push(h2("8.7 Failure modes & fallbacks"));
children.push(makeTableCustom(
  ["Failure", "Detection", "Fallback"],
  [
    ["Tier 1 unreachable (LM Studio down)", "connection refused / 8s timeout", "Promote Tier 2 to handle every candidate; alert"],
    ["Tier 1 schema-invalid", "Pydantic ValidationError", "Hold(schema_invalid_t1); do not escalate"],
    ["Tier 2 escalation timeout (2s)", "tenacity exhausted retries", "Use Tier 1 result alone (t1_fallback_t2)"],
    ["Tier 2 disagreement with Tier 1", "merge logic", "Hold(tier_disagreement)"],
    ["Daily Tier 2 budget exhausted", "counter at cap", "Use Tier 1 alone (t1_only_budget_exhausted)"],
    ["Anthropic API down (5xx)", "after 3 retries", "Use Tier 1 alone if Tier 2; else Hold(api_failure_t1)"],
    ["Out-of-range field (e.g. confidence=150)", "Pydantic validator", "Clamp to bounds; proceed"],
    ["Over-long string", "Pydantic validator", "Truncate to max-3 + '...'; proceed"],
  ],
  [2900, 2200, 4260],
));
children.push(p("All paths produce either a valid LLMDecision or a synthetic Hold. The signal engine never raises."));

children.push(pageBreak());

// ===== 9. Phase Plan =====
children.push(h1("9. Phase Plan"));
children.push(p("The implementation proceeds in named phases. Each phase produces a coherent, demonstrable artifact and ends with a sign-off doc + commit."));

children.push(h2("9.1 Completed"));
children.push(makeTableCustom(
  ["Phase", "Artifact", "Status"],
  [
    ["Tiered architecture design", "LLM_SIGNAL_INTERFACE.md, HARDWARE_PLATFORM.md, M2_REPLAY_HARNESS_DESIGN.md", "Done (commit eb93360 + 6e02186)"],
    ["Phase 0 scaffolding", "strategy/llm/ package + 41 mocked-test assertions", "Done"],
    ["Phase B - real prompt + full LLMContext", "prompts.py + 38-field LLMContext + smoke test", "Done"],
  ],
  [2400, 5000, 1960],
));

children.push(h2("9.2 In progress / next"));
children.push(makeTableCustom(
  ["Phase", "Description", "Effort"],
  [
    ["Phase A - shadow mode", "Wire signal_engine.evaluate into main.py; record decisions to SQLite without acting; cost ~$2-5/day", "1 day"],
    ["M2 replay harness", "Point-in-time historical replay of 30-90 days; per-tier output recording; comparison report vs base", "4 days"],
    ["M3 live signal engine refinement", "Async tier orchestration, rate limiting, budget tracking; replace stub paths", "2 days"],
    ["M4 backtest comparison", "Compare LLM-driven decisions to base over the same period; win rate, P&L, drawdown, regime stratification", "1 day"],
    ["M5 deploy decision", "If quality criteria met, swap trader-prod from rule-based to LLM-driven on the same fork", "0.5 day"],
  ],
  [2200, 5760, 1400],
));

children.push(h2("9.3 Workstation arrival"));
children.push(p(
  "When the workstation is delivered and configured, the LLM model fork transitions from cloud-only stand-in to local Tier 1. The transition is a config change (one line) plus an LM Studio model load. No code changes."
));
children.push(numbered("Install LM Studio (already pre-installed on Puget build)"));
children.push(numbered("Download Qwen 3.6-27B Instruct 4-bit weights"));
children.push(numbered("Start LM Studio server on localhost:1234"));
children.push(numbered("Edit config/settings.yaml: llm.t1.backend from haiku_stand_in to qwen_local; llm.t1.model_id to the LM Studio model identifier"));
children.push(numbered("Restart the trader; verify logs show \"Tier 1: qwen_local\""));
children.push(numbered("Re-run M2 replay against Qwen as Tier 1; compare to the cloud-tier baseline already produced"));

// ===== 10. Open Questions / Future Work =====
children.push(h1("10. Open Questions / Future Work"));

children.push(bulletRich([
  new TextRun({ text: "v1.1 prompt with few-shot examples. ", bold: true }),
  new TextRun({ text: "Current v1.0 prompt is too short for Anthropic's prompt cache to engage on Haiku (~2048 token minimum). Adding three to five worked few-shot examples to the system block pushes the prefix above the minimum and is independently valuable for decision quality on edge cases the system prompt does not anticipate." }),
]));

children.push(bulletRich([
  new TextRun({ text: "PM RVOL threshold recalibration. ", bold: true }),
  new TextRun({ text: "The current pm_rvol_thresholds.json has 30 large-cap names from the base watchlist. Once the gap-and-go fork's Russell 2000 universe is producing live data, recalibrate the per-ticker thresholds on small/mid-cap names." }),
]));

children.push(bulletRich([
  new TextRun({ text: "VIX availability. ", bold: true }),
  new TextRun({ text: "Polygon's index data coverage is uneven; VIX may be unavailable through the existing feed. The market_regime_label currently falls back to 'unknown' if VIX is missing. Worth investigating whether to add a separate index data feed or accept the gap." }),
]));

children.push(bulletRich([
  new TextRun({ text: "GitHub SSH on the VPS. ", bold: true }),
  new TextRun({ text: "trader-prod cannot git clone from GitHub via SSH; deploys go through git archive + scp + tar. Setting up an SSH key on the VPS and registering it with GitHub would simplify future deploys, though the current pattern works." }),
]));

children.push(bulletRich([
  new TextRun({ text: "pytest config drift. ", bold: true }),
  new TextRun({ text: "Async tests in the gap-and-go fork fail under pytest-asyncio strict mode because they lack the @pytest.mark.asyncio decorator. Adding asyncio_mode = auto to pyproject.toml fixes all 15 failures. Same fix should land in the LLM model fork before it grows async tests." }),
]));

children.push(bulletRich([
  new TextRun({ text: "Tier 3 cost ceiling. ", bold: true }),
  new TextRun({ text: "Full Opus labeling on a 60-day, 500-symbol, 78-cycle replay is ~140K calls and ~$200-400 with caching. Sample-rate parameter exists in ReplayConfig (t3_sample_rate); consider 0.1 for first replay run, 1.0 only after the harness is proven." }),
]));

children.push(bulletRich([
  new TextRun({ text: "Position-management evaluations. ", bold: true }),
  new TextRun({ text: "When holding a position, evaluate every cycle (~12-78 calls/day per held position) or rely on the bracket stop alone? Initial proposal: evaluate held positions every 15 min, not every 5 min." }),
]));

// ===== Appendix: file structure =====
children.push(pageBreak());
children.push(h1("Appendix A: Repository Structure"));

children.push(code("trading-model-llm/"));
children.push(code("|-- main.py                        # Asyncio orchestrator (inherited from base)"));
children.push(code("|-- requirements.txt               # Base dependencies"));
children.push(code("|-- requirements-llm.txt           # LLM-specific dependencies"));
children.push(code("|-- config/"));
children.push(code("|   `-- settings.yaml              # Includes llm: block"));
children.push(code("|-- analysis/"));
children.push(code("|   |-- sentiment.py               # Existing Haiku-based sentiment scorer"));
children.push(code("|   |-- indicators.py              # SMA, RSI, MACD, ADX, VWAP, gap-and-go logic"));
children.push(code("|   `-- futures_walls.py           # Dormant (Databento canceled)"));
children.push(code("|-- data/                          # Market data feeds"));
children.push(code("|-- execution/"));
children.push(code("|   `-- alpaca_orders.py           # Bracket orders, flatten routine"));
children.push(code("|-- strategy/"));
children.push(code("|   |-- signal_engine.py           # Rule-based combiner (inherited)"));
children.push(code("|   |-- risk.py                    # Position cap, ATR stops, validate_order"));
children.push(code("|   |-- signals/                   # Rule-based signal modules"));
children.push(code("|   `-- llm/                       # LLM signal generator (this work)"));
children.push(code("|       |-- types.py"));
children.push(code("|       |-- clients.py"));
children.push(code("|       |-- prompts.py"));
children.push(code("|       |-- escalation.py"));
children.push(code("|       |-- merge.py"));
children.push(code("|       |-- signal_engine.py"));
children.push(code("|       `-- factory.py"));
children.push(code("|-- scripts/"));
children.push(code("|   |-- verify_anthropic_client.py"));
children.push(code("|   |-- verify_llm_factory.py"));
children.push(code("|   |-- verify_signal_engine.py"));
children.push(code("|   |-- verify_prompts.py"));
children.push(code("|   `-- smoke_test_haiku.py"));
children.push(code("|-- tests/                         # pytest suite"));
children.push(code("`-- docs/"));
children.push(code("    |-- LLM_MODEL_CHARTER.md"));
children.push(code("    |-- LLM_SIGNAL_INTERFACE.md"));
children.push(code("    |-- HARDWARE_PLATFORM.md"));
children.push(code("    |-- M2_REPLAY_HARNESS_DESIGN.md"));
children.push(code("    `-- LLM_TRADING_MODEL_OVERVIEW.docx   <- this document"));

// ----------------------------------------------------------------------
// Build document
// ----------------------------------------------------------------------

const doc = new Document({
  creator: "Trading Platform Project",
  title: "LLM Trading Model - Implementation Overview",
  styles: {
    default: { document: { run: { font: "Arial", size: 22 } } }, // 11pt default
    paragraphStyles: [
      {
        id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 32, bold: true, font: "Arial", color: "1F3864" },
        paragraph: { spacing: { before: 360, after: 180 }, outlineLevel: 0 },
      },
      {
        id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 26, bold: true, font: "Arial", color: "2E75B6" },
        paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 1 },
      },
      {
        id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 22, bold: true, font: "Arial", color: "555555" },
        paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 2 },
      },
    ],
  },
  numbering: {
    config: [
      {
        reference: "bullets",
        levels: [
          {
            level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 720, hanging: 360 } } },
          },
          {
            level: 1, format: LevelFormat.BULLET, text: "◦", alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 1440, hanging: 360 } } },
          },
        ],
      },
      {
        reference: "numbers",
        levels: [{
          level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } },
        }],
      },
    ],
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 }, // US Letter
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
      },
    },
    headers: {
      default: new Header({
        children: [new Paragraph({
          alignment: AlignmentType.RIGHT,
          children: [new TextRun({ text: "LLM Trading Model - Implementation Overview", size: 18, color: "888888" })],
        })],
      }),
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [
            new TextRun({ text: "Page ", size: 18, color: "888888" }),
            new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "888888" }),
            new TextRun({ text: " of ", size: 18, color: "888888" }),
            new TextRun({ children: [PageNumber.TOTAL_PAGES], size: 18, color: "888888" }),
          ],
        })],
      }),
    },
    children,
  }],
});

const outPath = process.argv[2] || "LLM_TRADING_MODEL_OVERVIEW.docx";
Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync(outPath, buffer);
  console.log(`Wrote ${outPath} (${buffer.length} bytes)`);
}).catch(err => {
  console.error("Failed:", err);
  process.exit(1);
});
