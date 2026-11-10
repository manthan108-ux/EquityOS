import {
  DEFAULT_PREFERENCES,
  type MissingAssessment,
  type Readiness,
  type Report,
  type Requirement,
  type SessionStatus,
  type Upload,
} from "@/services/research";

export const STEPS = [
  "IDENTITY",
  "SOURCES",
  "DOCUMENTS",
  "EXTRACTION",
  "EVIDENCE",
  "CALCULATION",
  "PEERS",
  "SCORING",
  "READINESS",
  "REPORT",
];

export const session = (over: Partial<SessionStatus> = {}): SessionStatus => ({
  id: "s1",
  company_id: "c1",
  query: "SBIN",
  status: "AWAITING_DOCUMENTS",
  workflow: "ASSISTED",
  eligibility: "ASSISTED",
  readiness_score: 65.5,
  choice: null,
  preferences: DEFAULT_PREFERENCES,
  preference_log: [],
  last_error: null,
  updated_at: "2026-11-10T10:00:00Z",
  steps: STEPS.map((s) => ({
    step: s,
    label: `Step ${s}`,
    status: s === "SOURCES" ? "SKIPPED" : "DONE",
    attempts: 1,
    error: null,
    resumed_without_rerun: s === "EVIDENCE",
    finished_at: null,
  })),
  ...over,
});

export const nim: Requirement = {
  id: "interest_earned",
  label: "Interest earned",
  importance: "CRITICAL",
  status: "MISSING",
  why_it_matters: "Interest earned minus interest expended is net interest income.",
  affects: "net interest income, net interest margin",
  sections: ["financial_performance"],
  document: "bank-format profit and loss account",
  document_types: ["quarterly_result"],
  preferred_period: "latest available quarter",
  basis_required: "consolidated (your choice; never switched automatically)",
  official_source: "company investor-relations page, or the BSE/NSE filing",
  alternative: null,
  upload_helps: true,
  action: "UPLOAD",
  reason: "no validated value is stored for this figure",
};

export const otherBasis: Requirement = {
  ...nim,
  id: "net_profit",
  label: "Net profit",
  action: "CHANGE_BASIS_OR_UPLOAD",
  reason: "not available on the consolidated basis you chose; standalone figures exist",
  other_basis_available: { basis: "STANDALONE", period_end: "2026-06-30" },
};

export const readiness = (over: Partial<Readiness> = {}): Readiness => ({
  profile_label: "Bank",
  score: 65.5,
  eligibility: "ASSISTED",
  confidence: "LOW",
  reasons: ["2 critical item(s) missing or stale: Interest earned, Net profit"],
  open_validation_errors: 0,
  components: {
    financial_completeness: {
      label: "Financial data completeness",
      score: 70,
      applicable: true,
      weight: 0.2,
      detail: "14 of 19 financial requirements available",
    },
    sector_metrics: {
      label: "Sector-specific metrics",
      score: 30,
      applicable: true,
      weight: 0.25,
      detail: "2 of 5 sector-specific requirements available",
    },
  },
  thresholds: { min_score: 70, min_component: 40 },
  counts: {
    requirements: 19,
    available: 14,
    critical_missing: 2,
    important_missing: 0,
    optional_missing: 0,
  },
  sections: [],
  ...over,
});

export const missing = (over: Partial<MissingAssessment> = {}): MissingAssessment => ({
  eligibility: "ASSISTED",
  reasons: [],
  missing: { CRITICAL: [nim, otherBasis], IMPORTANT: [], OPTIONAL: [] },
  resolved: [
    {
      id: "nim",
      label: "Net interest margin (NIM)",
      importance: "CRITICAL",
      found: {
        metric: "RATIOS.net_interest_margin",
        value: "3.11",
        unit: "percent",
        period_end: "2026-09-30",
        period_type: "QUARTERLY",
        basis: null,
        document_id: "d1",
        page: 2,
        quality: "MEDIUM",
        dimension: "domestic",
      },
    },
  ],
  documents_held: [
    { title: "SBI Press Release", doc_type: "press_release", financial_period: "FY2027-Q2", extraction: "COMPLETED" },
  ],
  options: { continue_with_available: true },
  ...over,
});

export const upload = (over: Partial<Upload> = {}): Upload => ({
  id: "u1",
  filename: "results.pdf",
  status: "ACCEPTED",
  reject_reason: null,
  category: "quarterly_result",
  fiscal_year: "FY2027",
  quarter: "Q2",
  basis: "CONSOLIDATED",
  accounting_standard: "IND_AS",
  issuer_status: "CONFIRMED",
  extraction_status: "COMPLETED",
  validation_status: "PASSED",
  visibility: "SHARED",
  version: 1,
  uploaded_at: "2026-11-10T10:00:00Z",
  ambiguities: [],
  conflicts: [],
  resolved_requirements: ["nim"],
  steps: [],
  warnings: [],
  ...over,
});

export const report = (over: Partial<Report> = {}): Report => ({
  id: "r1",
  version: 1,
  mode: "AVAILABLE_EVIDENCE",
  readiness_score: 65.5,
  missing_critical: 2,
  missing_important: 0,
  created_at: "2026-11-10T10:00:00Z",
  complete: false,
  content: {
    meta: {
      banner: "Available-evidence report: 2 critical and 0 important item(s) are not available.",
      complete: false,
      company: "State Bank of India",
      as_of: "2026-11-10",
      preferences: { ...DEFAULT_PREFERENCES, period_label: "latest available quarter" },
    },
    readiness: {
      score: 65.5,
      eligibility: "ASSISTED",
      confidence: "LOW",
      note: "research readiness measures the evidence, not the company",
    },
    sections: [
      {
        key: "sector_analysis",
        title: "Sector-specific analysis",
        status: "PARTIAL",
        items: [
          {
            requirement: "nim",
            label: "Net interest margin (NIM)",
            importance: "CRITICAL",
            value: "3.11",
            unit: "percent",
            period_end: "2026-09-30",
            basis: null,
            dimension: "domestic",
            period_note: null,
            source: { evidence_id: 42, document_id: "d1", page: 2 },
          },
        ],
        gaps: [
          {
            requirement: "interest_earned",
            label: "Interest earned",
            importance: "CRITICAL",
            reason: "no validated value is stored for this figure",
            affects: "net interest income",
          },
        ],
      },
    ],
    limitations: {
      missing_critical: [
        { requirement: "interest_earned", label: "Interest earned", importance: "CRITICAL", reason: null, affects: "nii" },
      ],
      missing_important: [],
      missing_optional: [],
      unavailable_calculations: [],
      incomplete_valuation: [],
      unavailable_scores: null,
      limits_on_conclusions: ["No conclusion is drawn on interest earned: nii cannot rest on it."],
    },
  },
  ...over,
});
