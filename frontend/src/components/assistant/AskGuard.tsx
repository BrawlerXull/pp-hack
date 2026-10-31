"use client";

// Ask Guard: the dashboard's AI assistant (POST /assistant/chat, Gemini on the backend). It looks things up on
// its own; anything that changes the shop comes back as a proposal card, and only Confirm sends that card's
// request, to the same endpoint the matching dashboard button uses (so roles and safety rules still apply).

import { Check, Loader2, Send, Sparkles, Trash2, X } from "lucide-react";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useSWRConfig } from "swr";
import { api, ApiError, type AssistantReply, type ChatRole, type Me, type Proposal } from "@/lib/api";

type CardState = "open" | "running" | "done" | "failed" | "cancelled";
type Msg = {
  id: string;
  role: ChatRole;
  text: string;
  proposals?: (Proposal & { state: CardState; error?: string })[];
  lookedUp?: string[];
};

const STORE = "cg_ask_guard";
const MAX_HISTORY = 30;

const LOOKUP_LABEL: Record<string, string> = {
  get_overview: "shop status",
  list_incidents: "incidents",
  get_incident: "incident details",
  list_pending_approvals: "waiting refunds",
  list_disputes: "disputes",
  list_devices: "blocked devices",
  get_settings: "settings",
  list_recent_checkouts: "recent checkouts",
};

const STARTERS = ["What's happening right now?", "Is anything waiting for me?", "Explain the last incident", "Run a card-testing simulation"];

const newId = () => Math.random().toString(36).slice(2, 10);

function load(): Msg[] {
  try {
    return JSON.parse(sessionStorage.getItem(STORE) || "[]") as Msg[];
  } catch {
    return [];
  }
}

export function AskGuardButton({ onClick, open }: { onClick: () => void; open: boolean }) {
  return (
    <button
      id="ask-guard-button"
      type="button"
      onClick={onClick}
      aria-expanded={open}
      aria-controls="ask-guard"
      className="inline-flex h-9 items-center gap-2 rounded-lg bg-guard px-3 text-sm font-semibold text-white shadow-sm hover:bg-guard-dark"
    >
      <Sparkles className="size-4" aria-hidden />
      <span className="hidden sm:inline">Ask Guard</span>
      <span className="sr-only sm:hidden">Ask Guard, the AI assistant</span>
    </button>
  );
}

export function AskGuardPanel({ open, onClose, me }: { open: boolean; onClose: () => void; me: Me | undefined }) {
  const pathname = usePathname();
  const { mutate } = useSWRConfig();
  // Closed panels render nothing, so reading sessionStorage here can't make the server and client HTML differ.
  const [messages, setMessages] = useState<Msg[]>(() => (typeof window === "undefined" ? [] : load()));
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const viewer = me?.role === "viewer";

  // The conversation survives page changes and reloads in this tab (sessionStorage), never another device.
  useEffect(() => {
    try {
      sessionStorage.setItem(STORE, JSON.stringify(messages.slice(-MAX_HISTORY)));
    } catch {
      /* storage blocked: the chat still works for this page */
    }
  }, [messages]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages, busy]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const ask = useCallback(
    async (text: string, history: Msg[]) => {
      const question: Msg = { id: newId(), role: "user", text };
      const next = [...history, question];
      setMessages(next);
      setDraft("");
      setBusy(true);
      setError(null);
      try {
        const out = await api<AssistantReply>("/assistant/chat", {
          method: "POST",
          json: { messages: next.slice(-MAX_HISTORY).map(({ role, text }) => ({ role, text })), page: pathname },
        });
        setMessages((m) => [
          ...m,
          { id: newId(), role: "assistant", text: out.reply, lookedUp: out.looked_up, proposals: out.proposals.map((p) => ({ ...p, state: "open" })) },
        ]);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Can't reach Checkout Guard. Try again in a moment.");
      } finally {
        setBusy(false);
      }
    },
    [pathname],
  );

  function submit(e?: React.FormEvent) {
    e?.preventDefault();
    const text = draft.trim();
    if (text && !busy) void ask(text, messages);
  }

  function setCard(msgId: string, cardId: string, patch: { state: CardState; error?: string }) {
    setMessages((m) => m.map((x) => (x.id === msgId ? { ...x, proposals: x.proposals?.map((p) => (p.id === cardId ? { ...p, ...patch } : p)) } : x)));
  }

  // The outcome goes into the conversation as a note, so the next answer knows what was confirmed.
  const note = (text: string) => setMessages((m) => [...m, { id: newId(), role: "note", text }]);

  async function confirm(msgId: string, p: Proposal) {
    setCard(msgId, p.id, { state: "running" });
    try {
      await api(p.request.path, { method: p.request.method, json: p.request.body ?? undefined });
      setCard(msgId, p.id, { state: "done" });
      note(`Confirmed and done: ${p.title}`);
      void mutate(() => true); // every dashboard view refreshes with the change
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Can't reach Checkout Guard.";
      setCard(msgId, p.id, { state: "failed", error: message });
      note(`Tried "${p.title}" but it failed: ${message}`);
    }
  }

  function cancel(msgId: string, p: Proposal) {
    setCard(msgId, p.id, { state: "cancelled" });
    note(`Cancelled: ${p.title}`);
  }

  if (!open) return null;
  return (
    <aside
      id="ask-guard"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 flex flex-col bg-white sm:inset-y-0 sm:right-0 sm:left-auto sm:w-[420px] sm:border-l sm:border-line sm:shadow-2xl"
    >
      <header className="flex h-14 shrink-0 items-center justify-between gap-2 border-b border-line px-4">
        <div className="flex items-center gap-2">
          <span className="flex size-7 items-center justify-center rounded-lg bg-guard">
            <Sparkles className="size-4 text-white" aria-hidden />
          </span>
          <div className="leading-tight">
            <h2 id={titleId} className="text-[15px] font-semibold text-ink-1">
              Ask Guard
            </h2>
            <p className="text-[11px] text-ink-3">AI assistant · changes wait for your confirmation</p>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {messages.length > 0 && (
            <button
              type="button"
              onClick={() => {
                setMessages([]);
                setError(null);
              }}
              className="rounded-md p-2 text-ink-3 hover:bg-gray-100 hover:text-ink-1"
            >
              <Trash2 className="size-4" aria-hidden />
              <span className="sr-only">Clear the conversation</span>
            </button>
          )}
          <button type="button" onClick={onClose} className="rounded-md p-2 text-ink-3 hover:bg-gray-100 hover:text-ink-1">
            <X className="size-4" aria-hidden />
            <span className="sr-only">Close Ask Guard</span>
          </button>
        </div>
      </header>

      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4" aria-live="polite" aria-busy={busy}>
        {messages.length === 0 && (
          <div className="pt-4">
            <p className="text-sm text-ink-2">
              Ask about attacks, refunds, disputes or your settings. I can also do things for you, like approving a refund or pausing the guard: you confirm each change first.
            </p>
            <ul className="mt-4 space-y-2">
              {STARTERS.map((s) => (
                <li key={s}>
                  <button
                    type="button"
                    onClick={() => void ask(s, messages)}
                    disabled={busy}
                    className="w-full rounded-lg border border-line px-3 py-2 text-left text-sm text-ink-1 hover:border-guard hover:bg-blue-50"
                  >
                    {s}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {messages.map((m) =>
          m.role === "user" ? (
            <div key={m.id} className="flex justify-end">
              <p className="max-w-[85%] rounded-2xl rounded-br-md bg-guard px-3.5 py-2 text-sm whitespace-pre-wrap text-white">{m.text}</p>
            </div>
          ) : m.role === "note" ? (
            <p key={m.id} className="text-center text-xs text-ink-3">
              {m.text}
            </p>
          ) : (
            <div key={m.id} className="space-y-2">
              {m.lookedUp && m.lookedUp.length > 0 && (
                <p className="text-[11px] text-ink-3">Looked at: {m.lookedUp.map((t) => LOOKUP_LABEL[t] ?? t).join(", ")}</p>
              )}
              <div className="prose-report max-w-[95%] rounded-2xl rounded-bl-md bg-gray-100 px-3.5 py-2 text-sm text-ink-1 [&_p]:my-1">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.text}</ReactMarkdown>
              </div>
              {m.proposals?.map((p) => (
                <ProposalCard key={p.id} p={p} viewer={viewer} onConfirm={() => confirm(m.id, p)} onCancel={() => cancel(m.id, p)} />
              ))}
            </div>
          ),
        )}

        {busy && (
          <p className="flex items-center gap-2 text-sm text-ink-3">
            <Loader2 className="size-4 animate-spin" aria-hidden /> Looking into it…
          </p>
        )}
        {error && (
          <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
            {error}
          </p>
        )}
        <div ref={endRef} />
      </div>

      <form onSubmit={submit} className="shrink-0 border-t border-line p-3">
        <label htmlFor={`${titleId}-input`} className="sr-only">
          Message Ask Guard
        </label>
        <div className="flex items-end gap-2 rounded-xl border border-line-strong px-3 py-2 focus-within:border-guard focus-within:ring-2 focus-within:ring-guard/20">
          <textarea
            id={`${titleId}-input`}
            ref={inputRef}
            rows={1}
            value={draft}
            maxLength={4000}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder="Ask or tell Guard what to do…"
            className="max-h-32 min-h-6 flex-1 resize-none bg-transparent text-sm text-ink-1 placeholder:text-ink-3 focus:outline-none"
          />
          <button type="submit" disabled={busy || !draft.trim()} className="rounded-lg bg-guard p-1.5 text-white hover:bg-guard-dark disabled:opacity-40">
            <Send className="size-4" aria-hidden />
            <span className="sr-only">Send</span>
          </button>
        </div>
        <p className="mt-1.5 text-[11px] text-ink-3">Gemini can make mistakes: check each change before you confirm it.</p>
      </form>
    </aside>
  );
}

function ProposalCard({
  p,
  viewer,
  onConfirm,
  onCancel,
}: {
  p: Proposal & { state: CardState; error?: string };
  viewer: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const locked = viewer && p.owner_only;
  return (
    <div className={`rounded-xl border p-3.5 ${p.danger ? "border-red-200 bg-red-50/40" : "border-blue-200 bg-blue-50/40"}`}>
      <p className="text-[11px] font-semibold tracking-wide text-ink-3 uppercase">Needs your confirmation</p>
      <p className="mt-1 text-sm font-semibold text-ink-1">{p.title}</p>
      <p className="mt-0.5 text-[13px] text-ink-2">{p.detail}</p>
      <div className="mt-3 flex flex-wrap items-center gap-2" aria-live="polite">
        {p.state === "open" || p.state === "running" ? (
          locked ? (
            <p className="text-[13px] text-ink-3">Only an owner can confirm this.</p>
          ) : (
            <>
              <button
                type="button"
                onClick={onConfirm}
                disabled={p.state === "running"}
                className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-60 ${
                  p.danger ? "bg-red-600 hover:bg-red-700" : "bg-guard hover:bg-guard-dark"
                }`}
              >
                {p.state === "running" ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Check className="size-4" aria-hidden />} Confirm
              </button>
              <button
                type="button"
                onClick={onCancel}
                disabled={p.state === "running"}
                className="rounded-lg border border-line bg-white px-3 py-1.5 text-sm font-medium text-ink-1 hover:bg-gray-50"
              >
                Cancel
              </button>
            </>
          )
        ) : p.state === "done" ? (
          <p className="flex items-center gap-1.5 text-sm font-medium text-emerald-700">
            <Check className="size-4" aria-hidden /> Done
          </p>
        ) : p.state === "failed" ? (
          <p role="alert" className="text-sm text-red-700">
            Didn&apos;t work: {p.error}
          </p>
        ) : (
          <p className="text-sm text-ink-3">Cancelled</p>
        )}
      </div>
    </div>
  );
}
