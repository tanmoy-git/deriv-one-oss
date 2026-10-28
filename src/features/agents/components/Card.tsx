import { type ReactNode } from "react";

export function Card({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <section className="bg-white border border-slate-200 rounded-xl p-5 space-y-3">
      <header>
        <h3 className="text-sm font-bold text-slate-900">{title}</h3>
        {hint && <p className="text-xs text-slate-500 mt-1 leading-relaxed">{hint}</p>}
      </header>
      {children}
    </section>
  );
}
