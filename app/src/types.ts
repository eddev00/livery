// Shapes returned by the local engine API.
export type Status = "PASS" | "WARN" | "FAIL";

export interface Rect { page: number; x: number; y: number; w: number; h: number }
export interface TableShape { columns: number; header_rows: number; header_spans: { row: number; col: number; rowspan: number; colspan: number }[]; body_rows: number }
export interface MapElement { id: string; kind: string; label: string; rects: Rect[]; selectable: boolean; level?: number; table?: TableShape }
export interface SelectionMap { page: { width: number; height: number }; pages: number; elements: MapElement[]; unmapped: string[] }

export interface Version { v: number; status: string; error?: string; title?: string; report_status?: Status; elements?: number; file_name: string; imported_at: string }
export interface BuildStatus { state: "queued" | "running" | "done" | "failed"; revision: string; report_status?: Status; error?: string }
/** Whether the user marked the document done, and whether the accepted design still renders it the same. */
export interface ReviewState {
  state: "open" | "done" | "checking" | "recheck";
  approved?: { revision: string; version: number; at: string };
  /** why a done document needs another look: a new Word file, changed pages, or the approved render is gone */
  reason?: "version" | "design" | "unavailable";
  pages?: number[];
  changes?: { id: string; note: string; kind: string }[];
}
export interface DocumentInfo {
  id: string; name: string; current: number; versions: Version[]; version: Version;
  build: BuildStatus | null; report_status: Status | null; has_original: boolean; has_twin: boolean;
  review: ReviewState;
}
export interface ProjectStats {
  documents: number;
  review: Record<ReviewState["state"], number>;
  recheck: { doc: string; name: string; reason: string; pages: number[] }[];
  verification: Record<Status | "working", number>;
  rules: { name: string; expect: string | null; matched: number; documents: number; outside: string[] }[];
  unused_rules: string[];
}
export interface ProjectSummary {
  id: string; name: string; documents: number; updated?: string; accepted: string;
  health: { ready: number; failing: number; working: number };
}
export interface Treatment { kind: "heading" | "paragraph" | "table"; style: Record<string, unknown>; description?: string }
export interface Revision { id: string; parent: string | null; created: string; kind: string; note: string; reverts?: string }
export interface DesignInfo {
  accepted: string; history: string[]; can_undo: boolean;
  revisions: Revision[];
  /** accepted changes, oldest first; reverted_by names the later change that removed one */
  changes: (Revision & { reverted_by: string | null })[];
  treatments: Record<string, Treatment>; assignments: Record<string, string>; overrides: Record<string, Record<string, unknown>>;
}
export interface Proposal {
  id: string; base: string; status: "pending" | "accepted" | "discarded" | "stale"; origin: string; note: string;
  created: string; stale: boolean; documents: string[]; focus: string[];
  scope: { scope: "global" | "elements"; files: string[]; treatments: string[]; refs: string[]; documents: string[]; mapping?: boolean };
  /** set when the import mapping changed: element styles carried to the re-imported documents */
  remap?: { docs: string[]; dropped: string[] } | null;
  real: Record<string, BuildStatus | { state: "not rendered" }>;
  /** done documents this proposal touches: would accepting it change their pages? */
  done_impact?: Record<string, { state: "pending" | "same" | "changed"; pages?: number[] }>;
  synthetic?: { ok: boolean; problems: string[]; docs: Record<string, { sample: string; pages?: string[]; errors?: string[] }> } | null;
}
export interface Check { name: string; status: Status; summary: string; details?: string[]; data?: Record<string, unknown> }
export interface Report { status: Status; checks: Check[] }
export interface AgentStatus {
  provider: string; title: string; company: string; installed: boolean; binary?: string; version?: string;
  sandbox: boolean; sandbox_message?: string; logged_in: boolean; auth_method?: string; account?: string; error?: string;
}
export interface LoginHelp { provider: string; connect: boolean; secrets: { name: string; label: string }[] }
export interface Selection { doc: string; element: string; label: string; kind: string }
export interface ChatEntry { role: "user" | "agent" | "activity" | "error" | "approval" | "notice"; text: string; ts?: string; attachments?: string[]; id?: string; kind?: string }
export interface Job { id: string; kind: string; state: "queued" | "running" | "done" | "failed"; message: string; doc?: string; error?: string; result?: unknown; proposal?: string }
