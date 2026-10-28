import { type ReactNode } from "react";

export function ActionButton({
  onClick,
  disabled,
  children,
  icon,
  primary,
  destructive,
}: {
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
  icon?: ReactNode;
  primary?: boolean;
  destructive?: boolean;
}) {
  const base = "flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50 border";
  const variant = destructive
    ? "bg-white border-red-200 text-red-700 hover:bg-red-50"
    : primary
      ? "bg-slate-900 border-slate-900 text-white hover:bg-slate-700"
      : "bg-white border-slate-200 text-slate-700 hover:bg-slate-50";
  return (
    <button onClick={onClick} disabled={disabled} className={`${base} ${variant}`}>
      {icon}
      {children}
    </button>
  );
}
