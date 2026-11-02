import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { errorMessage } from "../api/errors";

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div className="error" role="alert">
      {errorMessage(error)}
    </div>
  );
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <p className="muted" aria-live="polite">{what}…</p>;
}

/** Plain text where `backtick` parts are shown as code (used for server-written hints). */
export function RichText({ text }: { text: string }) {
  return (
    <>
      {text.split("`").map((part, i) => (i % 2 === 1 ? <code key={i}>{part}</code> : <span key={i}>{part}</span>))}
    </>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="muted">{children}</p>;
}

export function StateBadge({ state }: { state: string }) {
  return <span className={`badge state-${state}`}>{state}</span>;
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="secondary"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } catch {
          window.prompt("Copy this:", text);
        }
      }}
    >
      {done ? "Copied" : label}
    </button>
  );
}

/**
 * Confirmation for destructive actions. When `typeToConfirm` is set the person must type
 * that exact text (e.g. the group name) before the confirm button is enabled.
 */
export function ConfirmDialog(props: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  typeToConfirm?: string;
  busy?: boolean;
  error?: unknown;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [typed, setTyped] = useState("");
  const titleId = useId();
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (props.open) {
      setTyped("");
      cancelRef.current?.focus();
    }
  }, [props.open]);
  useEffect(() => {
    if (!props.open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && props.onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [props.open, props.onCancel]);
  if (!props.open) return null;
  const ok = !props.typeToConfirm || typed === props.typeToConfirm;
  return (
    <div className="overlay">
      <div className="dialog" role="alertdialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 id={titleId}>{props.title}</h2>
        <div>{props.children}</div>
        {props.typeToConfirm && (
          <label>
            Type <code>{props.typeToConfirm}</code> to confirm
            <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          </label>
        )}
        <ErrorBox error={props.error} />
        <div className="row end">
          <button type="button" className="secondary" ref={cancelRef} onClick={props.onCancel}>
            Cancel
          </button>
          <button type="button" className="danger" disabled={!ok || props.busy} onClick={props.onConfirm}>
            {props.busy ? "Working…" : props.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function Tabs<T extends string>(props: {
  tabs: { id: T; label: string }[];
  active: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {props.tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={props.active === t.id}
          className={props.active === t.id ? "tab active" : "tab"}
          onClick={() => props.onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
